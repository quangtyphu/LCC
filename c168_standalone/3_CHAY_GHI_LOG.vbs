' Gan script ghi log (sau khi da mo Chrome bang 2_MO_CHROME_DEBUG.vbs)
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "cmd /k cd /d """ & dir & """ && set PYTHONIOENCODING=utf-8 && python c168_register.py --manual --cdp http://127.0.0.1:9222"
sh.Run cmd, 1, True
