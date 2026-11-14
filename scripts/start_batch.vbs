' Open the JackPotts control panel and start a batch with its saved settings,
' with no console window. Run it through explorer.exe from another program
' (a Claude session) so the batch is not tied to that program's processes.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = root
shell.Run """" & root & "\.venv\Scripts\pythonw.exe"" -m balatro_bot.gui --start", 0, False
