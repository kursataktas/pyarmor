import logging
import marshal
import os
import shutil
import struct
import sys
import tempfile
import zlib

from importlib._bootstrap_external import _code_to_timestamp_pyc
from subprocess import check_call

from PyInstaller.archive.writers import ZlibArchiveWriter, CArchiveWriter
from PyInstaller.archive.readers import CArchiveReader
try:
    from PyInstaller.loader.pyimod02_archive import ZlibArchiveReader
except ModuleNotFoundError:
    # Since 5.3
    from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader
from PyInstaller.compat import BYTECODE_MAGIC, is_darwin, is_linux, is_win
from PyInstaller.building.utils import get_code_object, strip_paths_in_code


# Type codes for PYZ PYZ entries
PYZ_ITEM_MODULE = 0
PYZ_ITEM_PKG = 1
PYZ_ITEM_DATA = 2
PYZ_ITEM_NSPKG = 3  # PEP-420 namespace package

# Type codes for CArchive TOC entries
PKG_ITEM_BINARY = 'b'  # binary
PKG_ITEM_DEPENDENCY = 'd'  # runtime option
PKG_ITEM_PYZ = 'z'  # zlib (pyz) - frozen Python code
PKG_ITEM_ZIPFILE = 'Z'  # zlib (pyz) - frozen Python code
PKG_ITEM_PYPACKAGE = 'M'  # Python package (__init__.py)
PKG_ITEM_PYMODULE = 'm'  # Python module
PKG_ITEM_PYSOURCE = 's'  # Python script (v3)
PKG_ITEM_DATA = 'x'  # data
PKG_ITEM_RUNTIME_OPTION = 'o'  # runtime option
PKG_ITEM_SPLASH = 'l'  # splash resources

# Path suffix for extracted contents
EXTRACT_SUFFIX = '_extracted'


logger = logging.getLogger('repack')


class CArchiveReader2(CArchiveReader):

    # Cookie - holds some information for the bootloader.
    #
    #   typedef struct _cookie {
    #       char magic[8]; /* 'MEI\014\013\012\013\016' */
    #       uint32_t len;  /* len of entire package */
    #       uint32_t TOC;  /* pos (rel to start) of TableOfContents */
    #       int  TOClen;   /* length of TableOfContents */
    #       int  pyvers;   /* new in v4 */
    #       char pylibname[64];    /* Filename of Python dynamic library. */
    #   } COOKIE;
    #

    # TOC entry:
    #
    #   typedef struct _toc {
    #       int  structlen;  /* len of this one - including full len of name */
    #       uint32_t pos;    /* pos rel to start of concatenation */
    #       uint32_t len;    /* len of the data (compressed) */
    #       uint32_t ulen;   /* len of data (uncompressed) */
    #       char cflag;      /* is it compressed (really a byte) */
    #       char typcd;      /* type code -'b' binary, 'z' zlib, 'm' module,
    #                         * 's' script (v3),'x' data, 'o' runtime option  */
    #       char name[1];    /* the name to save it as */
    #                        /* starting in v5, we stretch this out to a mult of 16 */
    #   } TOC;
    #

    def find_magic_pattern(self, fp, magic_pattern):
        # Start at the end of file, and scan back-to-start
        fp.seek(0, os.SEEK_END)
        end_pos = fp.tell()

        # Scan from back
        SEARCH_CHUNK_SIZE = 8192
        magic_offset = -1
        while end_pos >= len(magic_pattern):
            start_pos = max(end_pos - SEARCH_CHUNK_SIZE, 0)
            chunk_size = end_pos - start_pos
            # Is the remaining chunk large enough to hold the pattern?
            if chunk_size < len(magic_pattern):
                break
            # Read and scan the chunk
            fp.seek(start_pos, os.SEEK_SET)
            buf = fp.read(chunk_size)
            pos = buf.rfind(magic_pattern)
            if pos != -1:
                magic_offset = start_pos + pos
                break
            # Adjust search location for next chunk; ensure proper overlap
            end_pos = start_pos + len(magic_pattern) - 1

        return magic_offset

    def get_cookie_info(self, fp):
        magic = getattr(self, '_COOKIE_MAGIC_PATTERN',
                        getattr(self, 'MAGIC', b'MEI\014\013\012\013\016'))
        cookie_pos = self.find_magic_pattern(fp, magic)

        cookie_format = getattr(self, '_COOKIE_FORMAT',
                                getattr(self, '_cookie_format', '!8sIIii64s'))
        cookie_size = struct.calcsize(cookie_format)

        fp.seek(cookie_pos, os.SEEK_SET)
        return struct.unpack(cookie_format, fp.read(cookie_size))

    def get_toc(self):
        if isinstance(self.toc, dict):
            return self.toc
        return {entry[-1]: entry[:-1] for entry in self.toc}

    def open_pyzarchive(self, name):
        if hasattr(self, 'open_embedded_archive'):
            return self.open_embedded_archive(self, name)

        ndx = self.toc.find(name)
        (dpos, dlen, ulen, flag, typcd, nm) = self.toc.get(ndx)
        return ZlibArchiveReader(self.path, self.pkg_start + dpos)

    def get_logical_toc(self, buildpath, obfpath):
        logical_toc = []

        for name, entry in self.get_toc().items():
            *_, flag, typecode = entry
            if typecode == PKG_ITEM_PYMODULE:
                source = os.path.join(obfpath, name + '.py')
            elif typecode == PKG_ITEM_PYSOURCE:
                source = os.path.join(obfpath, name + '.py')
            elif typecode == PKG_ITEM_PYPACKAGE:
                source = os.path.join(obfpath, name, '__init__.py')
            elif typecode == PKG_ITEM_PYZ:
                source = os.path.join(buildpath, name)
            elif typecode in (PKG_ITEM_DEPENDENCY, PKG_ITEM_RUNTIME_OPTION):
                source = ''
            else:
                source = None
            if source and not os.path.exists(source):
                source = None
            item = name, source, flag, typecode
            logical_toc.append(item)

        return logical_toc


class CArchiveWriter2(CArchiveWriter):

    def __init__(self, archive_path, logical_toc, pylib_name, src_arch):
        self._carch = src_arch
        super().__init__(archive_path, logical_toc, pylib_name)

    def _write_rawdata(self, name, typecode, compress):
        rawdata = fix_extract(self._carch.extract(name))
        if hasattr(self, '_write_blob'):
            # Since 5.0
            self._write_blob(rawdata, name, typecode, compress)
            return

        with tempfile.NamedTemporaryFile() as f:
            f.write(rawdata)
            f.flush()
            if typecode in (PKG_ITEM_PYSOURCE, PKG_ITEM_PYMODULE,
                            PKG_ITEM_PYPACKAGE):
                super().add((name, f.name, compress, PKG_ITEM_DATA))
                tc = self.toc.data[-1]
                self.toc.data[-1] = tc[:-2] + (typecode, tc[-1])
            else:
                super().add((name, f.name, compress, typecode))

    def add(self, entry):
        name, source, compress, typecode = entry[:4]
        if source is None:
            self._write_rawdata(name, typecode, compress)
        else:
            logger.info('replace entry "%s"', name)
            print(entry)
            super().add(entry)

    def _write_entry(self, fp, entry):
        '''For PyInstaller 5.10+'''
        name, source, compress, typecode = entry[:4]
        if source is None:
            rawdata = self._carch.extract(name)
            self._write_blob(fp, rawdata, name, typecode, compress)
        else:
            super()._write_entry(fp, entry)


def fix_extract(data):
    return data[1] if isinstance(data, tuple) else data


def extract_pyzarchive(name, pyzarch, output):
    dirname = os.path.join(output, name + EXTRACT_SUFFIX)
    os.makedirs(dirname, exist_ok=True)

    for name, (typecode, offset, length) in pyzarch.toc.items():
        # Prevent writing outside dirName
        filename = name.replace('..', '__').replace('.', os.path.sep)
        if typecode == PYZ_ITEM_PKG:
            filepath = os.path.join(dirname, filename, '__init__.pyc')
        elif typecode == PYZ_ITEM_MODULE:
            filepath = os.path.join(dirname, filename + '.pyc')
        elif typecode == PYZ_ITEM_DATA:
            filepath = os.path.join(dirname, filename)
        else:
            continue
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'wb') as f:
            f.write(_code_to_timestamp_pyc(fix_extract(pyzarch.extract(name))))

    return dirname


def repack_pyzarchive(pyzpath, pyztoc, obfpath, rtpath, cipher=None):
    # logic_toc tuples: (name, src_path, typecode)
    #   `name` is the name without suffix)
    #   `src_path` is name of the file from which the resource is read
    #   `typecode` is the Analysis-level TOC typecode (`PYMODULE` or `DATA`)
    logical_toc = []
    code_dict = {}
    extra_path = pyzpath + EXTRACT_SUFFIX
    prefix = len(obfpath) + 1

    def compile_item(name, fullpath):
        if not os.path.exists(fullpath):
            fullpath = fullpath + 'c'
        if os.path.exists(fullpath):
            logger.info('replace item "%s"', name)
            with open(fullpath, 'r') as f:
                code_dict[name] = compile(f.read(), '<frozen %s>' % name, 'exec')
        else:
            fullpath = os.path.join(extra_path, fullpath[prefix:])
            with open(fullpath, 'rb') as f:
                f.seek(16)
                code_dict[name] = marshal.load(f)

    rtname = os.path.basename(rtpath)
    filepath = os.path.join(rtpath, '__init__.py')
    logical_toc.append((rtname, filepath, 'PYMODULE'))
    compile_item(rtname, filepath)

    for name, (typecode, offset, length) in pyztoc.items():
        filename = name.replace('..', '__').replace('.', os.path.sep)
        if typecode == PYZ_ITEM_PKG:
            filepath = os.path.join(obfpath, filename, '__init__.py')
            compile_item(name, filepath)
        elif typecode == PYZ_ITEM_MODULE:
            filepath = os.path.join(obfpath, filename + '.py')
            compile_item(name, filepath)
        elif typecode == PYZ_ITEM_DATA:
            filepath = os.path.join(extra_path, filename)
        elif typecode == PYZ_ITEM_NSPKG:
            filepath = '-'
        else:
            raise ValueError('unknown PYZ item type "%s"' % typecode)
        logical_toc.append((name, filepath, 'DATA'
                            if typecode == PYZ_ITEM_DATA else 'PYMODULE'))

    ZlibArchiveWriter(pyzpath, logical_toc, code_dict, cipher=cipher)


def repack_carchive(executable, pkgname, buildpath, obfpath, rtentry):
    pkgarch = CArchiveReader2(executable)
    with open(executable, 'rb') as fp:
        *_, pylib_name = pkgarch.get_cookie_info(fp)
    logical_toc = pkgarch.get_logical_toc(buildpath, obfpath)
    if rtentry is not None:
        logical_toc.append(rtentry)
    pkgfile = os.path.join(buildpath, pkgname)
    CArchiveWriter2(pkgfile, logical_toc, pylib_name.decode('utf-8'), pkgarch)


def repack_executable(executable, buildpath, pkgname, codesign=None):
    logger.info('repacking EXE "%s"', executable)
    pkgfile = os.path.join(buildpath, pkgname)

    if is_darwin:
        import PyInstaller.utils.osx as osxutils
        if hasattr(osxutils, 'remove_signature_from_binary'):
            logger.info("removing signature(s) from EXE")
            osxutils.remove_signature_from_binary(executable)

    if is_linux:
        logger.info('replace section "pydata" with "%s"', pkgname)
        check_call(['objcopy', '--update-section', 'pydata=%s' % pkgfile,
                    executable])
    else:
        reader = CArchiveReader2(executable)
        logger.info('replace PKG with "%s"', pkgname)
        with open(executable, 'r+b') as outf:
            info = reader.get_cookie_info(outf)
            offset = os.fstat(outf.fileno()).st_size - info[1]
            # Keep bootloader
            outf.seek(offset, os.SEEK_SET)

            # Write the patched archive
            with open(pkgfile, 'rb') as infh:
                shutil.copyfileobj(infh, outf, length=64*1024)

            outf.truncate()

        if is_darwin:
            # Fix Mach-O header for codesigning on OS X.
            logger.info('fixing EXE for code signing')
            import PyInstaller.utils.osx as osxutils
            osxutils.fix_exe_for_code_signing(executable)

            if hasattr(osxutils, 'sign_binary'):
                logger.info("re-signing the EXE")
                osxutils.sign_binary(executable, identity=codesign)

        elif is_win:
            # Set checksum to appease antiviral software.
            from PyInstaller.utils.win32 import winutils
            if hasattr(winutils, 'set_exe_checksum'):
                winutils.set_exe_checksum(executable)

    logger.info('generate patched bundle "%s" successfully', executable)


class Repacker:

    def __init__(self, executable, buildpath):
        self.executable = executable
        self.buildpath = buildpath
        self.extract_carchive(executable, buildpath)

    def extract_carchive(self, executable, buildpath, clean=True):
        logger.info('extracting bundle "%s"', executable)
        shutil.rmtree(self.buildpath)
        os.makedirs(self.buildpath)

        contents = []
        pyz_tocs = {}
        pkgarch = CArchiveReader2(executable)
        pkgtoc = pkgarch.get_toc()

        for name, toc_entry in pkgtoc.items():
            logger.debug('extract %s', name)
            *_, typecode = toc_entry

            if typecode == PKG_ITEM_PYZ:
                pyzarch = pkgarch.open_pyzarchive(name)
                pyz_tocs[name] = pyzarch.toc
                contents.append(extract_pyzarchive(name, pyzarch, buildpath))
                # pyzdata = fix_extract(pkgarch.extract(name))
                # with open(os.path.join(buildpath, name), 'wb') as f:
                #     f.write(pyzdata)

        self.contents = contents
        self.pyz_tocs = pyz_tocs
        self.one_file_mode = len(pkgtoc) > 10

    def repack(self, obfpath, rtname, entry=None):
        executable = self.executable
        logger.info('repack bundle "%s"', executable)

        obfpath = os.path.normpath(obfpath)
        logger.info('obfuscated scripts at "%s"', obfpath)

        name, ext = os.path.splitext(os.path.basename(executable))
        entry = name if entry is None else entry
        logger.info('entry script name is "%s.py"', entry)

        rtpath = os.path.join(obfpath, rtname)
        for item in self.contents:
            if item.endswith(EXTRACT_SUFFIX):
                pyzpath = item[:-len(EXTRACT_SUFFIX)]
                pyztoc = self.pyz_tocs[os.path.basename(pyzpath)]
                logger.info('repack "%s"', os.path.basename(pyzpath))
                repack_pyzarchive(pyzpath, pyztoc, obfpath, rtpath)

        for x in os.listdir(rtpath):
            ext = os.path.splitext(x)[-1]
            if x.startswith('pyarmor_runtime') and ext in ('.so', '.pyd'):
                rtbinary = os.path.join(rtpath, x)
                rtbinname = os.path.join(rtname, x)
                break
        else:
            raise RuntimeError('no pyarmor runtime files found')

        if is_darwin:
            from PyInstaller.depend import dylib
            logger.debug('mac_set_relative_dylib_deps "%s"', rtbinname)
            dylib.mac_set_relative_dylib_deps(rtbinary, rtbinname)

        if self.one_file_mode:
            rtentry = rtbinname, rtbinary, 1, 'b'
        else:
            dest = os.path.join(os.path.dirname(executable), rtname)
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(rtbinary, dest)
            rtentry = None

        pkgname = 'PKG-patched'
        repack_carchive(executable, pkgname, self.buildpath, obfpath, rtentry)
        repack_executable(executable, self.buildpath, pkgname)


class CArchiveWriterLocal:
    """
    Writer for PyInstaller's CArchive (PKG) archive.
    This archive contains all files that are bundled within an executable; a PYZ (ZlibArchive), DLLs, Python C
    extensions, and other data files that are bundled in onefile mode.
    The archive can be read from either C (bootloader code at application's run-time) or Python (for debug purposes).
    """
    _COOKIE_MAGIC_PATTERN = b'MEI\014\013\012\013\016'

    # For cookie and TOC entry structure, see `PyInstaller.archive.readers.CArchiveReader`.
    _COOKIE_FORMAT = '!8sIIii64s'
    _COOKIE_LENGTH = struct.calcsize(_COOKIE_FORMAT)

    _TOC_ENTRY_FORMAT = '!iIIIBB'
    _TOC_ENTRY_LENGTH = struct.calcsize(_TOC_ENTRY_FORMAT)

    _COMPRESSION_LEVEL = 9  # zlib compression level

    def __init__(self, filename, entries, pylib_name, pkgarch):
        """
        filename
            Target filename of the archive.
        entries
            An iterable containing entries in the form of tuples: (dest_name, src_name, compress, typecode), where
            `dest_name` is the name under which the resource is stored in the archive (and name under which it is
            extracted at runtime), `src_name` is name of the file from which the resouce is read, `compress` is a
            boolean compression flag, and `typecode` is the Analysis-level TOC typecode.
        pylib_name
            Name of the python shared library.
        """
        self.pkgarch = pkgarch

        with open(filename, "wb") as fp:
            # Write entries' data and collect TOC entries
            toc = []
            for entry in entries:
                name, source, compress, typecode = entry[:4]
                if source is None:
                    rawdata = fix_extract(self.pkgarch.extract(name))
                    toc_entry = self._write_blob(fp, rawdata, name, typecode, compress)
                else:
                    toc_entry = self._write_entry(fp, entry)
                toc.append(toc_entry)

            # Write TOC
            toc_offset = fp.tell()
            toc_data = self._serialize_toc(toc)
            toc_length = len(toc_data)

            fp.write(toc_data)

            # Write cookie
            archive_length = toc_offset + toc_length + self._COOKIE_LENGTH
            pyvers = sys.version_info[0] * 100 + sys.version_info[1]
            cookie_data = struct.pack(
                self._COOKIE_FORMAT,
                self._COOKIE_MAGIC_PATTERN,
                archive_length,
                toc_offset,
                toc_length,
                pyvers,
                pylib_name.encode('ascii'),
            )

            fp.write(cookie_data)

    def _write_entry(self, fp, entry):
        dest_name, src_name, compress, typecode = entry

        # Ensure forward slashes in paths are on Windows converted to back slashes '\\', as on Windows the bootloader
        # works only with back slashes.
        dest_name = os.path.normpath(dest_name)
        if is_win and os.path.sep == '/':
            # When building under MSYS, the above path normalization uses Unix-style separators, so replace them
            # manually.
            dest_name = dest_name.replace(os.path.sep, '\\')

        if typecode == 'o':
            return self._write_blob(fp, b"", dest_name, typecode)
        elif typecode == 'd':
            # Dependency; merge src_name (= reference path prefix) and dest_name (= name) into single-string format that
            # is parsed by bootloader.
            return self._write_blob(fp, b"", f"{src_name}:{dest_name}", typecode)
        elif typecode == 's':
            # If it is a source code file, compile it to a code object and marshal the object, so it can be unmarshalled
            # by the bootloader.
            code = get_code_object(dest_name, src_name)
            code = strip_paths_in_code(code)
            return self._write_blob(fp, marshal.dumps(code), dest_name, typecode, compress=compress)
        elif typecode in ('m', 'M'):
            # Read the PYC file
            with open(src_name, "rb") as in_fp:
                data = in_fp.read()
            assert data[:4] == BYTECODE_MAGIC
            # Skip the PYC header, load the code object.
            code = marshal.loads(data[16:])
            code = strip_paths_in_code(code)
            # These module entries are loaded and executed within the bootloader, which requires only the code
            # object, without the PYC header.
            return self._write_blob(fp, marshal.dumps(code), dest_name, typecode, compress=compress)
        else:
            return self._write_file(fp, src_name, dest_name, typecode, compress=compress)

    def _write_blob(self, out_fp, blob: bytes, dest_name, typecode, compress=False):
        """
        Write the binary contents (**blob**) of a small file to the archive and return the corresponding CArchive TOC
        entry.
        """
        data_offset = out_fp.tell()
        data_length = len(blob)
        if compress:
            blob = zlib.compress(blob, level=self._COMPRESSION_LEVEL)
        out_fp.write(blob)

        return (data_offset, len(blob), data_length, int(compress), typecode, dest_name)

    def _write_file(self, out_fp, src_name, dest_name, typecode, compress=False):
        """
        Stream copy a large file into the archive and return the corresponding CArchive TOC entry.
        """
        data_offset = out_fp.tell()
        data_length = os.stat(src_name).st_size
        with open(src_name, 'rb') as in_fp:
            if compress:
                tmp_buffer = bytearray(16 * 1024)
                compressor = zlib.compressobj(self._COMPRESSION_LEVEL)
                while True:
                    num_read = in_fp.readinto(tmp_buffer)
                    if not num_read:
                        break
                    out_fp.write(compressor.compress(tmp_buffer[:num_read]))
                out_fp.write(compressor.flush())
            else:
                shutil.copyfileobj(in_fp, out_fp)

        return (data_offset, out_fp.tell() - data_offset, data_length, int(compress), typecode, dest_name)

    @classmethod
    def _serialize_toc(cls, toc):
        serialized_toc = []
        for toc_entry in toc:
            data_offset, compressed_length, data_length, compress, typecode, name = toc_entry

            # Encode names as UTF-8. This should be safe as standard python modules only contain ASCII-characters (and
            # standard shared libraries should have the same), and thus the C-code still can handle this correctly.
            name = name.encode('utf-8')
            name_length = len(name) + 1  # add 1 for a '\0'

            # Align to 16-byte boundary so xplatform C can read
            entry_length = cls._TOC_ENTRY_LENGTH + name_length
            if entry_length % 16 == 0:
                name_padding = b'\0'
            else:
                padding_length = 16 - (entry_length % 16)
                name_padding = b'\0' * padding_length
            name_length += padding_length

            # Serialize
            serialized_entry = struct.pack(
                cls._TOC_ENTRY_FORMAT + f"{name_length}s",
                cls._TOC_ENTRY_LENGTH + name_length,
                data_offset,
                compressed_length,
                data_length,
                compress,
                ord(typecode),
                name + name_padding,
            )
            serialized_toc.append(serialized_entry)

        return b''.join(serialized_toc)


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(message)s',
    )
    dest = '.pyarmor/pack'
    os.makedirs(dest, exist_ok=True)
