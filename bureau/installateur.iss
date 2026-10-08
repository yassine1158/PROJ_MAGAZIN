; Installateur Windows : ISCC /DVersion=1.0.0 bureau\installateur.iss
#ifndef Version
  #define Version "1.0.0"
#endif
#define Nom "Magasin SI BETON"

[Setup]
AppId={{6E0B8F4C-3E0B-4B57-9C4B-5A1F2C9E7D11}
AppName=Magasin SI BÉTON
AppVersion={#Version}
AppPublisher=SI BÉTON
DefaultDirName={autopf}\{#Nom}
DefaultGroupName=Magasin SI BÉTON
DisableProgramGroupPage=yes
; Installation possible sans droits administrateur (pour l'utilisateur courant).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\build\installateur
OutputBaseFilename=Installer-Magasin-SI-BETON-{#Version}
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
Name: "{group}\Magasin SI BÉTON"; Filename: "{app}\{#Nom}.exe"
Name: "{autodesktop}\Magasin SI BÉTON"; Filename: "{app}\{#Nom}.exe"; Tasks: bureau

[Run]
Filename: "{app}\{#Nom}.exe"; Description: "Ouvrir le logiciel maintenant"; Flags: nowait postinstall skipifsilent

; Les données (base, photos) restent dans %LOCALAPPDATA%\Magasin SI BETON : elles sont gardées
; lors d'une mise à jour ou d'une désinstallation.
