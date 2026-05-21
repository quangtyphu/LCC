' Lan dau hoac muon sach hoan toan: dong Chrome + xoa profile + mo lai trang dang ky
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
profile = sh.ExpandEnvironmentStrings("%TEMP%\c168-chrome-profile")

sh.Popup "Dong Chrome va xoa profile cu (2 giay)...", 2, "C168", 64
sh.Run "taskkill /IM chrome.exe /F", 0, True
WScript.Sleep 2000
If fso.FolderExists(profile) Then
  On Error Resume Next
  fso.DeleteFolder profile, True
  On Error GoTo 0
End If

sh.Run """" & dir & "\2_MO_CHROME_DEBUG.vbs""", 1, False
