; OmniCam PC Windows installer (Inno Setup 6)
#define MyAppName "OmniCam PC"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "OmniCam"
#define MyAppURL "https://github.com/pixelash459/OmniCam"
#define MyAppExeName "OmniCam.exe"

[Setup]
AppId={{8F3C2A91-6B47-4E1D-9C5A-0D2E7B18F4A3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\OmniCam
DefaultGroupName=OmniCam
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=OmniCam-PC-{#MyAppVersion}-Setup
SetupIconFile=omnicam.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile=
InfoBeforeFile=
CloseApplications=yes
RestartIfNeededByRun=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "firewall"; Description: "Allow OmniCam through Windows Firewall (needed to find the phone)"; GroupDescription: "Network:"; Flags: checkedonce

[Files]
Source: "..\dist\OmniCam\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\OmniCam PC"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\OmniCam PC"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""OmniCam PC UDP"""; Flags: runhidden skipifdoesntexist; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""OmniCam PC UDP"" dir=in action=allow protocol=UDP localport=9920-9921 program=""{app}\{#MyAppExeName}"" profile=any"; Flags: runhidden; Tasks: firewall
Filename: "{app}\{#MyAppExeName}"; Description: "Launch OmniCam PC now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""OmniCam PC UDP"""; Flags: runhidden; RunOnceId: "RemoveFirewall"
