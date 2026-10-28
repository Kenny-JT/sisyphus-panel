' Sisyphus Panel - start the server in the background on Windows:
' no console window, no taskbar button.
'   Double-click this file, or run:  wscript start-panel.vbs [--no-browser]
' If the panel is already running it just opens the page (nothing with --no-browser).
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
If PanelRunning() Then
    If InStr(args, "--no-browser") = 0 Then shell.Run "http://127.0.0.1:7878"
    WScript.Quit
End If

' panel.log only ever grows while the server runs (its cmd keeps it open), so
' rotate it here, when nothing has it open: past 1 MB it becomes panel.log.1.
Dim logPath
logPath = shell.CurrentDirectory & "\panel.log"
If fso.FileExists(logPath) Then
    If fso.GetFile(logPath).Size > 1048576 Then
        ' Best effort: if something holds the log open without sharing it (a log
        ' viewer, antivirus), keep appending to the big log rather than not starting.
        On Error Resume Next
        If fso.FileExists(logPath & ".1") Then fso.DeleteFile logPath & ".1", True
        fso.MoveFile logPath, logPath & ".1"
        On Error GoTo 0
    End If
End If
' Window style 0 = hidden; False = return immediately.
' py -3.12 pins the Windows Python (see start-panel.bat). -X utf8 keeps print()
' from failing on non-ASCII filenames once stdout is a file; -u keeps the log current.
shell.Run "cmd /c py -3.12 -X utf8 -u panel.py" & args & " >> panel.log 2>&1", 0, False

' The running server's cmd keeps panel.log open for writing, so if the log
' cannot be opened the panel is already up (a second copy could not start anyway).
' Not an HTTP probe of /health on purpose: antivirus heuristics flag a VBS that
' fetches a URL and then runs commands as a downloader.
Function PanelRunning()
    On Error Resume Next
    fso.OpenTextFile(shell.CurrentDirectory & "\panel.log", 8, True).Close
    PanelRunning = (Err.Number <> 0)
    On Error GoTo 0
End Function
