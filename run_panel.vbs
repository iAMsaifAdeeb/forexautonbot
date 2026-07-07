' Gold Genious - launch without black console window (used by Desktop shortcut)
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)

Function FindPython(base)
    If fso.FileExists(base & "\pythonw.exe") Then
        FindPython = base & "\pythonw.exe"
        Exit Function
    End If
    If fso.FileExists(base & "\python.exe") Then
        FindPython = base & "\python.exe"
        Exit Function
    End If
    FindPython = ""
End Function

py = FindPython(CreateObject("WScript.Shell").ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312")
If py = "" Then py = FindPython("C:\Program Files\Python312")
If py = "" Then py = FindPython("C:\Program Files\Python313")
If py = "" Then
    On Error Resume Next
    Set sh = CreateObject("WScript.Shell")
    sh.Run "cmd /c cd /d """ & dir & """ && run_panel.bat", 1, False
    WScript.Quit
End If

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
sh.Run """" & py & """ """ & dir & "\control_panel.py""", 1, False
