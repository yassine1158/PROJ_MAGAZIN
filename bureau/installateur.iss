; Installateur Windows : ISCC /DVersion=1.0.0 bureau\installateur.iss
#ifndef Version
  #define Version "1.0.0"
#endif
#define Nom "MagaStock"

[Setup]
AppId={{6E0B8F4C-3E0B-4B57-9C4B-5A1F2C9E7D11}
AppName={#Nom}
AppVersion={#Version}
AppPublisher={#Nom}
DefaultDirName={autopf}\{#Nom}
DefaultGroupName={#Nom}
DisableProgramGroupPage=yes
; Installation possible sans droits administrateur (pour l'utilisateur courant).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\build\installateur
OutputBaseFilename=Installer-{#Nom}-{#Version}
SetupIconFile=icone.ico
UninstallDisplayIcon={app}\{#Nom}.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "bureau"; Description: "Créer une icône sur le bureau"; GroupDescription: "Icônes :"

[Files]
Source: "..\build\dist\{#Nom}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#Nom}"; Filename: "{app}\{#Nom}.exe"
Name: "{autodesktop}\{#Nom}"; Filename: "{app}\{#Nom}.exe"; Tasks: bureau

[Run]
Filename: "{app}\{#Nom}.exe"; Description: "Ouvrir le logiciel maintenant"; Flags: nowait postinstall skipifsilent

[InstallDelete]
; Ancien nom du programme (version 1.0) : on retire l'ancien exécutable et ses raccourcis.
Type: files; Name: "{app}\Magasin SI BETON.exe"
Type: files; Name: "{autodesktop}\Magasin SI BÉTON.lnk"
Type: files; Name: "{group}\Magasin SI BÉTON.lnk"

; Les données (base, photos) restent dans %LOCALAPPDATA%\{#Nom} : elles sont gardées
; lors d'une mise à jour ou d'une désinstallation.
