import ctypes
from ctypes import wintypes
from pathlib import Path

src = Path(
    r"C:\Users\Quang\Documents\CMS\chrome_profiles_data\cp_mrefygry_a7ox5kxq\Default\Network\Cookies"
)
dst = Path(r"C:\Users\Quang\AppData\Local\Temp\ctypes_cookies_test.db")

kernel32 = ctypes.windll.kernel32
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID = ctypes.c_void_p(-1).value

for share_name, share in [
    ("ALL", 0x07),
    ("READ_WRITE", 0x03),
    ("READ", 0x01),
    ("NONE", 0x00),
]:
    handle = kernel32.CreateFileW(
        str(src),
        GENERIC_READ,
        share,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    err = kernel32.GetLastError()
    ok = handle and handle != INVALID
    print(share_name, "handle", ok, "err", err)
    if ok:
        size = ctypes.c_int64()
        kernel32.GetFileSizeEx(handle, ctypes.byref(size))
        print("  size", size.value)
        kernel32.CloseHandle(handle)
