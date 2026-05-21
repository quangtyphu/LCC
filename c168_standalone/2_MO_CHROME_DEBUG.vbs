' Mo Chrome che do debug (port 9222) - double-click file nay
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)

chrome = ""
If fso.FileExists("C:\Program Files\Google\Chrome\Application\chrome.exe") Then
  chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
ElseIf fso.FileExists(fso.BuildPath(sh.ExpandEnvironmentStrings("%LOCALAPPDATA%"), "Google\Chrome\Application\chrome.exe")) Then
  chrome = fso.BuildPath(sh.ExpandEnvironmentStrings("%LOCALAPPDATA%"), "Google\Chrome\Application\chrome.exe")
End If

If chrome = "" Then
  sh.Popup "Khong tim thay Chrome. Cai Google Chrome.", 10, "C168 LOI", 16
  WScript.Quit 1
End If

profile = sh.ExpandEnvironmentStrings("%TEMP%\c168-chrome-profile")
url = "https://c168b2.cc/home/register"
cmd = """" & chrome & """ --remote-debugging-port=9222 --user-data-dir=""" & profile & """ --no-first-run """ & url

sh.Run cmd, 1, False
WScript.Sleep 3000

' Kiem tra port
ok = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://127.0.0.1:9222/json/version", False
http.Send
If http.Status = 200 Then ok = True
On Error GoTo 0

If ok Then
  msg = "Chrome debug OK (port 9222)." & vbCrLf & vbCrLf & "Trong CMD chay:" & vbCrLf & "cd " & dir & vbCrLf & "python c168_register.py --manual --cdp http://127.0.0.1:9222"
  sh.Popup msg, 15, "C168 OK", 64
Else
  sh.Popup "Chrome mo nhung port 9222 chua san." & vbCrLf & "Chay 1_DONG_CHROME_CU.vbs roi chay lai file nay.", 12, "C168", 48
End If
