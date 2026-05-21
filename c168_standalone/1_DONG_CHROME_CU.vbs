' Dong het Chrome (neu dang mo) - double-click file nay
Set sh = CreateObject("WScript.Shell")
sh.Popup "Se dong tat Chrome trong 2 giay...", 2, "C168", 64
sh.Run "taskkill /IM chrome.exe /F", 0, True
WScript.Sleep 2000
sh.Popup "Xong. Bay gio double-click: 2_MO_CHROME_DEBUG.vbs", 5, "C168", 64
