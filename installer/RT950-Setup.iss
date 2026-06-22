; ============================================================================
;  RT-950 Pro BLE — one-click Windows installer
;
;  Builds RT950-Setup.exe. It bundles the CHIRP drivers + the BLE bridge/GUI,
;  and at install time downloads & silently installs everything they need:
;    * Python 3.10 (with the py/pyw launcher)         <- from python.org
;    * the bleak + pyserial packages (via pip)
;    * com0com (signed) + a COM10<->COM11 virtual pair <- from SourceForge
;
;  CHIRP itself is NOT installed (it can't be silenced reliably); the finish
;  page links the user to it.
;
;  Build:  see installer/build.md  (winget install JRSoftware.InnoSetup,
;          then  ISCC.exe RT950-Setup.iss)
;  Requires Inno Setup 6.1+ (for the built-in download support).
; ============================================================================

#define AppName     "RT-950 Pro BLE"
#define AppVersion  "1.0.0"
#define AppPublisher "Nivin Goonesekera (VK3NWG)"
#define AppURL      "https://github.com/nivingoonesekera/Chirp_BLE_Radtel-RT-950-Pro"

; Downloaded dependencies (verified canonical URLs)
#define PyVer       "3.10.11"
#define PyExe       "python-" + PyVer + "-amd64.exe"
#define PyUrl       "https://www.python.org/ftp/python/" + PyVer + "/" + PyExe
#define Com0comZip  "com0com-3.0.0.0-i386-and-x64-signed.zip"
#define Com0comUrl  "https://downloads.sourceforge.net/project/com0com/com0com/3.0.0.0/" + Com0comZip

[Setup]
AppId={{E1DF89C8-3E9F-4C89-9D43-AB3841830A96}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\RT-950 Pro BLE
DefaultGroupName=RT-950 Pro BLE
DisableProgramGroupPage=yes
OutputDir=..
OutputBaseFilename=RT950-Setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\icon.ico
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; com0com driver + all-users Python both need admin
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut for the Bridge GUI"; GroupDescription: "Shortcuts:"

[Files]
; Drivers (loaded into CHIRP via Load Module) + bridge/GUI + icons + docs.
; Sources are relative to this .iss (release/installer/) -> parent is release/.
Source: "..\radtel_rt950pro_BLE_int.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\radtel_rt950pro_BL.py";      DestDir: "{app}"; Flags: ignoreversion
Source: "..\ble_bridge.py";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\bridge_gui.py";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.ico";                   DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.png";                   DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";                  DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion
Source: "..\LICENSE";                    DestDir: "{app}"; Flags: ignoreversion

[Icons]
; GUI launcher — run under the windowed py launcher pinned to 3.10 so the GUI's
; sys.executable (which it uses to spawn ble_bridge.py) is the 3.10 that has
; bleak + pyserial installed.
Name: "{group}\RT-950 Pro Bridge";  Filename: "{win}\pyw.exe"; Parameters: "-3.10 ""{app}\bridge_gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"
Name: "{group}\RT-950 Driver Files"; Filename: "{app}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Uninstall RT-950 Pro BLE"; Filename: "{uninstallexe}"
Name: "{autodesktop}\RT-950 Pro Bridge"; Filename: "{win}\pyw.exe"; Parameters: "-3.10 ""{app}\bridge_gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
; 1) Python 3.10 (quiet, all-users, PATH + launcher).
Filename: "{tmp}\{#PyExe}"; \
  Parameters: "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_pip=1"; \
  StatusMsg: "Installing Python {#PyVer} ..."; Flags: waituntilterminated

; 2) bleak + pyserial into that 3.10 (the py launcher always lands in {win}).
Filename: "{win}\py.exe"; Parameters: "-3.10 -m pip install --upgrade bleak pyserial"; \
  StatusMsg: "Installing bleak + pyserial ..."; Flags: waituntilterminated runhidden

; 3) com0com: unzip the downloaded signed build, silent-install it, add a pair.
;    Only when com0com isn't already present (keeps re-runs from stacking pairs).
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Expand-Archive -LiteralPath '{tmp}\{#Com0comZip}' -DestinationPath '{tmp}\com0com' -Force"""; \
  StatusMsg: "Unpacking com0com ..."; Flags: waituntilterminated runhidden; Check: NeedCom0com
Filename: "{tmp}\com0com\setup.exe"; Parameters: "/S"; \
  StatusMsg: "Installing com0com driver ..."; Flags: waituntilterminated; Check: NeedCom0com
Filename: "{commonpf32}\com0com\setupc.exe"; Parameters: "install PortName=COM10 PortName=COM11"; \
  StatusMsg: "Creating the COM10 <-> COM11 virtual pair ..."; Flags: waituntilterminated runhidden; Check: NeedCom0com

[Code]
var
  DownloadPage: TDownloadWizardPage;
  Com0comPreInstalled: Boolean;

function NeedCom0com: Boolean;
begin
  Result := not Com0comPreInstalled;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing),
                                     SetupMessage(msgPreparingDesc), nil);
end;

function InitializeSetup: Boolean;
begin
  // If com0com is already installed we leave it (and its existing pairs) alone.
  Com0comPreInstalled := FileExists(ExpandConstant('{commonpf32}\com0com\setupc.exe'));
  Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  if CurPageID = wpReady then begin
    DownloadPage.Clear;
    DownloadPage.Add('{#PyUrl}', '{#PyExe}', '');
    if NeedCom0com then
      DownloadPage.Add('{#Com0comUrl}', '{#Com0comZip}', '');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
        Result := True;
      except
        if DownloadPage.AbortedByUser then
          Log('Download aborted by user.')
        else
          SuppressibleMsgBox(AddPeriod(GetExceptionMessage), mbCriticalError, MB_OK, IDOK);
        Result := False;
      end;
    finally
      DownloadPage.Hide;
    end;
  end else
    Result := True;
end;
