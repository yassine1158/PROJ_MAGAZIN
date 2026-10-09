; Installateur Windows : ISCC /DVersion=1.0.0 bureau\installateur.iss
; Fichier enregistré en UTF-8 avec BOM (pour que les accents soient bien lus).
#ifndef Version
  #define Version "1.0.0"
#endif
; Même nom que MUTEX dans config/produit.py (windows.yml le passe avec /DMutex=…).
#ifndef Mutex
  #define Mutex "MagaStockInstance"
#endif
#define Nom "MagaStock"
#define Site "https://yassine1158.github.io/PROJ_MAGAZIN/"
; Programme d'installation de WebView2 (Microsoft), téléchargé par windows.yml : inclus seulement s'il est là.
#define WebView2 AddBackslash(SourcePath) + "..\build\MicrosoftEdgeWebview2Setup.exe"

[Setup]
AppId={{6E0B8F4C-3E0B-4B57-9C4B-5A1F2C9E7D11}
AppName={#Nom}
AppVersion={#Version}
AppPublisher={#Nom}
AppPublisherURL={#Site}
AppSupportURL={#Site}
AppUpdatesURL={#Site}
; Le logiciel ouvert est détecté : l'installateur (et la désinstallation) demandent de le fermer.
AppMutex={#Mutex}
VersionInfoVersion={#Version}
VersionInfoCompany={#Nom}
VersionInfoDescription=Installation de {#Nom} (gestion de magasin et de stock)
VersionInfoProductName={#Nom}
DefaultDirName={autopf}\{#Nom}
DefaultGroupName={#Nom}
DisableProgramGroupPage=yes
; Installation pour l'utilisateur courant, sans question ni droits administrateur
; (les données sont de toute façon dans le dossier de l'utilisateur).
PrivilegesRequired=lowest
OutputDir=..\build\installateur
; Nom fixe : le lien …/releases/latest/download/MagaStock-Installation.exe donne toujours la dernière version.
OutputBaseFilename={#Nom}-Installation
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
#if FileExists(WebView2)
Source: "{#WebView2}"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not WebView2Present
#endif

[Icons]
Name: "{group}\{#Nom}"; Filename: "{app}\{#Nom}.exe"
Name: "{autodesktop}\{#Nom}"; Filename: "{app}\{#Nom}.exe"; Tasks: bureau

[Run]
#if FileExists(WebView2)
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installation de Microsoft Edge WebView2 (affichage du logiciel)…"; Flags: waituntilterminated; Check: not WebView2Present
#endif
Filename: "{app}\{#Nom}.exe"; Description: "Ouvrir le logiciel maintenant"; Flags: nowait postinstall skipifsilent

[InstallDelete]
; Ancien nom du programme (version 1.0) : on retire l'ancien exécutable et ses raccourcis.
Type: files; Name: "{app}\Magasin SI BETON.exe"
Type: files; Name: "{autodesktop}\Magasin SI BÉTON.lnk"
Type: files; Name: "{group}\Magasin SI BÉTON.lnk"

; Les données (base, photos) restent dans %LOCALAPPDATA%\{#Nom} : elles sont gardées
; lors d'une mise à jour ou d'une désinstallation.

[Code]
const
  CleWebView2 = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function WebView2Installe(Racine: Integer; Cle: String): Boolean;
var
  Valeur: String;
begin
  Result := RegQueryStringValue(Racine, Cle, 'pv', Valeur) and (Valeur <> '') and (Valeur <> '0.0.0.0');
end;

{ WebView2 déjà présent (Windows 11 et la plupart des Windows 10) : rien à installer. }
function WebView2Present: Boolean;
begin
  Result := WebView2Installe(HKLM, 'SOFTWARE\WOW6432Node\' + CleWebView2)
    or WebView2Installe(HKCU, 'Software\' + CleWebView2);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    MsgBox('{#Nom} a été désinstallé.' + #13#10#13#10 +
      'Vos données (articles, bons, photos, sauvegardes) sont conservées dans le dossier :' + #13#10 +
      ExpandConstant('{localappdata}\{#Nom}') + #13#10#13#10 +
      'Elles seront reprises automatiquement si vous réinstallez le logiciel.', mbInformation, MB_OK);
end;
