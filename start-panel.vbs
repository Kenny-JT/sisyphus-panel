' Sisyphus Panel - start the server in the background on Windows:
' no console window, no taskbar button.
'   Double-click this file, or run:  wscript start-panel.vbs [--no-browser]
' Output is appended to panel.log next to this file. Stop it with stop-panel.bat.
' To watch the log live in a console instead, use start-panel.bat.
Option Explicit
Dim fso, shell, args, i
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
args = ""
For i = 0 To WScript.Arguments.Count - 1
    args = args & " " & WScript.Arguments(i)
Next
' Window style 0 = hidden; False = return immediately.
' py -3.12 pins the Windows Python (see start-panel.bat). -X utf8 keeps print()
' from failing on non-ASCII filenames once stdout is a file; -u keeps the log current.
shell.Run "cmd /c py -3.12 -X utf8 -u panel.py" & args & " >> panel.log 2>&1", 0, False
