"""Portable contract tests for the isolated Win32 rename boundary.

They intentionally run on Linux with a fake kernel32 adapter.  They cannot
establish the real Windows filesystem's sharing/rename behavior; see the live
Windows qualification checklist in the module docstring and handoff.
"""

from dataclasses import replace
import ctypes
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd import win32  # noqa: E402


class FakeWin32:
    """Model handle lifetime, metadata, and no-replace rename separately."""

    def __init__(self):
        self.entries = {
            "C:\\": self.dirinfo(1),
            "C:\\incoming": self.dirinfo(2),
            "C:\\organized": self.dirinfo(3),
            "C:\\organized\\Movies": self.dirinfo(4),
            "C:\\incoming\\movie.mkv": self.fileinfo(55),
        }
        self.handles = {}
        self.next_handle = 100
        self.calls = []
        self.renames = []
        self.on_rename = None
        self.volume_ok = True
        self.directory_opens = []

    @staticmethod
    def dirinfo(file_id, *, volume=999, attrs=win32._FILE_ATTRIBUTE_DIRECTORY):
        return win32.FileInfo(volume, file_id, attrs, True, 0, 0)

    @staticmethod
    def fileinfo(file_id, *, volume=999, attrs=0, size=5, mtime_ns=123400):
        return win32.FileInfo(volume, file_id, attrs, False, size, mtime_ns)

    def require_local_drive(self, drive):
        self.calls.append(("drive", drive))
        if not self.volume_ok:
            raise win32.UnsafeWindowsMove("drive is not local NTFS/ReFS")

    def open(self, path, *, source=False, missing_ok=False, directory=False):
        self.calls.append(("open", path, source, missing_ok))
        if directory:
            self.directory_opens.append(path)
        if path not in self.entries:
            if missing_ok:
                return None
            raise OSError(f"missing {path}")
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = path
        return handle

    def info(self, handle):
        return self.entries[self.handles[handle]]

    def rename(self, source, destination_parent, leaf):
        src = self.handles[source]
        parent = self.handles[destination_parent]
        self.renames.append((source, destination_parent, leaf))
        self.calls.append(("rename", src, parent, leaf))
        if self.on_rename:
            self.on_rename(self)
        destination = parent.rstrip("\\") + "\\" + leaf
        if destination in self.entries:
            raise FileExistsError(destination)
        self.entries[destination] = self.entries.pop(src)

    def close(self, handle):
        self.calls.append(("close", handle))
        del self.handles[handle]


class Win32RenameTests(unittest.TestCase):
    source = r"C:\incoming\movie.mkv"
    destination = r"C:\organized\Movies\movie.mkv"

    def setUp(self):
        self.api = FakeWin32()

    def move(self, **kwargs):
        arguments = {"identity": (999, 55), "size": 5, "mtime_ns": 123400, "_api": self.api}
        arguments.update(kwargs)
        return win32.rename_noreplace(self.source, self.destination, **arguments)

    def test_happy_path_keeps_all_ancestors_locked_through_rename(self):
        def inspect(api):
            self.assertEqual(
                set(api.handles.values()),
                {"C:\\", r"C:\incoming", r"C:\organized", r"C:\organized\Movies", self.source},
            )
            self.assertEqual(api.handles[api.renames[-1][1]], r"C:\organized\Movies")
            self.assertEqual(api.renames[-1][2], "movie.mkv")

        self.api.on_rename = inspect
        self.move()
        self.assertNotIn(self.source, self.api.entries)
        self.assertIn(self.destination, self.api.entries)
        self.assertEqual(self.api.entries[self.destination].identity, (999, 55))
        self.assertEqual(self.api.handles, {})
        self.assertEqual(len(self.api.renames), 1)
        self.assertEqual(
            set(self.api.directory_opens),
            {"C:\\", r"C:\incoming", r"C:\organized", r"C:\organized\Movies"},
        )
        self.assertIn(("open", self.source, True, False), self.api.calls)
        self.assertIn(("open", self.destination, False, True), self.api.calls)

    def test_reparse_parent_or_source_is_never_renamed(self):
        for target in (r"C:\organized", self.source):
            with self.subTest(target=target):
                self.api = FakeWin32()
                info = self.api.entries[target]
                self.api.entries[target] = replace(
                    info, attributes=info.attributes | win32._FILE_ATTRIBUTE_REPARSE_POINT
                )
                with self.assertRaisesRegex(win32.UnsafeWindowsMove, "reparse"):
                    self.move()
                self.assertFalse(self.api.renames)
                self.assertFalse(self.api.handles)

    def test_identity_size_mtime_and_file_type_must_match_open_handle(self):
        changes = (
            ("identity", {"identity": (999, 56)}),
            ("size", {"size": 6}),
            ("mtime", {"mtime_ns": 111}),
        )
        for _description, kwargs in changes:
            with self.subTest(kwargs=kwargs):
                self.api = FakeWin32()
                with self.assertRaises(win32.UnsafeWindowsMove):
                    self.move(**kwargs)
                self.assertEqual(self.api.handles, {})
                self.assertFalse(self.api.renames)
        self.api = FakeWin32()
        self.api.entries[self.source] = self.api.dirinfo(55)
        with self.assertRaisesRegex(win32.UnsafeWindowsMove, "type"):
            self.move()

    def test_unavailable_or_inconsistent_file_ids_and_attributes_fail_closed(self):
        for changes in (
            {"volume": 0}, {"file_id": 0},
            {"attributes": win32._FILE_ATTRIBUTE_DIRECTORY},
        ):
            with self.subTest(changes=changes):
                self.api = FakeWin32()
                self.api.entries[self.source] = replace(self.api.entries[self.source], **changes)
                with self.assertRaises(win32.UnsafeWindowsMove):
                    self.move()
                self.assertFalse(self.api.handles)

    def test_cross_volume_and_unsupported_drive_are_refused(self):
        self.api.entries[r"C:\organized\Movies"] = self.api.dirinfo(4, volume=111)
        with self.assertRaisesRegex(win32.UnsafeWindowsMove, "volume"):
            self.move()
        self.assertEqual(self.api.handles, {})
        self.api = FakeWin32()
        self.api.volume_ok = False
        with self.assertRaisesRegex(win32.UnsafeWindowsMove, "drive"):
            self.move()
        self.assertFalse(self.api.calls[1:])

    def test_occupied_destination_is_refused_even_if_same_file_hardlink(self):
        for file_id in (55, 88):
            with self.subTest(file_id=file_id):
                self.api = FakeWin32()
                existing = self.api.fileinfo(file_id)
                self.api.entries[self.destination] = existing
                with self.assertRaisesRegex(win32.UnsafeWindowsMove, "already exists"):
                    self.move()
                self.assertIs(self.api.entries[self.destination], existing)
                self.assertIn(self.source, self.api.entries)
                self.assertFalse(self.api.renames)
                self.assertFalse(self.api.handles)

    def test_collision_created_after_destination_check_fails_without_replacement(self):
        other = self.api.fileinfo(77)

        def race(api):
            api.entries[self.destination] = other

        self.api.on_rename = race
        with self.assertRaises(FileExistsError):
            self.move()
        self.assertIs(self.api.entries[self.destination], other)
        self.assertIn(self.source, self.api.entries)
        self.assertFalse(self.api.handles)

    def test_errors_close_handles_and_leave_source_untouched(self):
        self.api.entries.pop(r"C:\organized\Movies")
        with self.assertRaises(OSError):
            self.move()
        self.assertFalse(self.api.handles)
        self.assertIn(self.source, self.api.entries)

    def test_rejects_ambiguous_or_unanchorable_paths(self):
        bad_paths = (
            r"\\server\share\movie.mkv", r"\\?\C:\incoming\movie.mkv",
            r"C:incoming\movie.mkv", r"C:\incoming\..\movie.mkv",
            r"C:\incoming\.\movie.mkv", r"C:\incoming\movie.mkv:other",
            "C:\\incoming\\movie.mkv ", r"C:\incoming\CON.txt",
            r"C:\incoming\\movie.mkv", r"D:\incoming\movie.mkv",
            self.destination,
        )
        for path in bad_paths:
            with self.subTest(path=path):
                self.api = FakeWin32()
                with self.assertRaises(win32.UnsafeWindowsMove):
                    win32.rename_noreplace(
                        path, self.destination, identity=(999, 55),
                        size=5, mtime_ns=123400, _api=self.api,
                    )
                self.assertFalse(self.api.calls)

    def test_rejects_portable_device_aliases_and_unicode_control_names(self):
        unsafe_leaves = (
            "CON", "con.txt", "PRN.log", "AUX", "NUL.bin",
            "COM1.exe", "com9", "LPT1.doc", "LPT9.txt",
            "COM\u00b9.txt", "COM\u00b2.txt", "COM\u00b3.txt",
            "LPT\u00b9.txt", "LPT\u00b2.txt", "LPT\u00b3.txt",
            "CONIN$", "CONIN$.txt", "conout$.log",
            "evil\u202eexe.txt", "hidden\u2066name", "zero\ufeffwidth",
            "control\x7fname", "surrogate\ud800name",
            "a<b.txt", "a>b.txt", "a?b.txt", "a*b.txt", "a|b.txt",
            'a"b.txt', "a:b.txt", "trailing.", "trailing ",
        )
        for leaf in unsafe_leaves:
            with self.subTest(leaf=repr(leaf)):
                for as_destination in (False, True):
                    self.api = FakeWin32()
                    source = self.source if as_destination else "C:\\incoming\\" + leaf
                    destination = (
                        "C:\\organized\\Movies\\" + leaf
                        if as_destination else self.destination
                    )
                    with self.assertRaises(win32.UnsafeWindowsMove):
                        win32.rename_noreplace(
                            source, destination, identity=(999, 55),
                            size=5, mtime_ns=123400, _api=self.api,
                        )
                    self.assertFalse(self.api.calls)

    def test_accepts_regular_unicode_names_in_path_parser(self):
        drive, parts = win32._path_parts("C:\\incoming\\caf\u00e9.mkv")
        self.assertEqual(drive, "C:")
        self.assertEqual(parts, ("incoming", "caf\u00e9.mkv"))

    def test_linux_host_without_injected_adapter_fails_closed(self):
        if sys.platform == "win32":
            self.skipTest("Win32 adapter is available on Windows")
        with self.assertRaisesRegex(win32.UnsafeWindowsMove, "unavailable"):
            win32.rename_noreplace(
                self.source, self.destination,
                identity=(999, 55), size=5, mtime_ns=123400,
            )

    def test_file_rename_info_abi_and_relative_utf16_name(self):
        # Portable fixed-width declarations must match the Win32 x86/x64 ABI,
        # including on Linux where c_wchar and c_ulong are wider.
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(win32._FILE_RENAME_INFO.RootDirectory.offset, 8 if pointer_size == 8 else 4)
        self.assertEqual(win32._FILE_RENAME_INFO.FileName.offset, 20 if pointer_size == 8 else 12)
        data, n = win32._rename_buffer(0xABC, "café.mkv")
        header = ctypes.cast(data, ctypes.POINTER(win32._FILE_RENAME_INFO)).contents
        encoded = "café.mkv".encode("utf-16le")
        self.assertEqual(header.RootDirectory, 0xABC)
        self.assertEqual(header.ReplaceIfExists, 0)
        self.assertEqual(header.FileNameLength, len(encoded))
        self.assertEqual(n, max(ctypes.sizeof(win32._FILE_RENAME_INFO), 20 + len(encoded) + 2))
        self.assertEqual(ctypes.string_at(ctypes.addressof(data) + win32._FILE_RENAME_INFO.FileName.offset, len(encoded)), encoded)

    def test_ctypes_adapter_uses_no_follow_no_share_delete_and_delete_access(self):
        api = object.__new__(win32._Win32Api)
        calls = []

        def create(*args):
            calls.append(args)
            return 1001

        api._create = create
        self.assertEqual(api.open(self.source, source=True), 1001)
        path, access, sharing, _security, disposition, flags, _template = calls[0]
        self.assertEqual(path, "\\\\?\\" + self.source)
        self.assertTrue(access & win32._DELETE)
        self.assertTrue(access & win32._FILE_READ_ATTRIBUTES)
        self.assertEqual(sharing, win32._FILE_SHARE_READ | win32._FILE_SHARE_WRITE)
        self.assertEqual(disposition, win32._OPEN_EXISTING)
        self.assertTrue(flags & win32._FILE_FLAG_OPEN_REPARSE_POINT)
        self.assertTrue(flags & win32._FILE_FLAG_BACKUP_SEMANTICS)

        api.open(self.destination)
        self.assertFalse(calls[1][1] & win32._DELETE)

        api.open(r"C:\organized", directory=True)
        self.assertTrue(calls[2][1] & win32._FILE_TRAVERSE)
        self.assertTrue(calls[2][1] & win32._FILE_READ_ATTRIBUTES)
        self.assertEqual(calls[2][2], win32._FILE_SHARE_READ | win32._FILE_SHARE_WRITE)

    def test_ctypes_adapter_uses_relative_parent_handle_and_nonreplace_class(self):
        api = object.__new__(win32._Win32Api)
        seen = []

        def set_info(source, kind, buffer, length):
            record = ctypes.cast(buffer, ctypes.POINTER(win32._FILE_RENAME_INFO)).contents
            filename = ctypes.string_at(
                ctypes.addressof(buffer) + win32._FILE_RENAME_INFO.FileName.offset,
                record.FileNameLength,
            ).decode("utf-16le")
            seen.append((source, kind, record.ReplaceIfExists, record.RootDirectory, filename, length))
            return 1

        api._set = set_info
        api.rename(101, 202, "target.mkv")
        self.assertEqual(seen[0][:5], (101, win32._FILE_RENAME_INFO_CLASS, 0, 202, "target.mkv"))
        self.assertGreaterEqual(seen[0][5], win32._FILE_RENAME_INFO.FileName.offset + 20)

    def test_native_rename_failure_identifies_rejected_operation(self):
        api = object.__new__(win32._Win32Api)
        api._set = lambda *args: 0
        api._win_error = lambda: OSError(87, "The parameter is incorrect")
        with self.assertRaisesRegex(win32.UnsafeWindowsMove, "FileRenameInfo.*87"):
            api.rename(101, 202, "target.mkv")


if __name__ == "__main__":
    unittest.main()
