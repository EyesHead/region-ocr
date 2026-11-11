"""Build one Windows online installer; heavyweight prerequisites stay upstream."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
VERSION = '1.8.6'


def build(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    plan = json.loads((HERE / 'install-plan.json').read_text('utf-8'))
    if plan['version'] != VERSION:
        raise ValueError('Installer version differs from pinned plan')
    subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', str(ROOT / 'local_web/launcher/build.ps1')], check=True)
    files = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z']).decode('utf-8').split('\0')
    files = {ROOT / name for name in files if name and (ROOT / name).is_file()}
    files.update(f for f in HERE.rglob('*') if f.is_file() and '__pycache__' not in f.parts
                 and 'tests' not in f.relative_to(HERE).parts
                 and f.name != 'environment-snapshot.json')
    files.add(ROOT / 'DocumentConverter.exe')
    files = {f for f in files if f.suffix != '.pyc' and f.name != 'environment-snapshot.json'
             and f.relative_to(ROOT).parts[0] not in {'regression', 'tests', 'docs'}
             and 'tests' not in f.relative_to(ROOT).parts}
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file():
        raise FileNotFoundError('The system .NET Framework C# compiler was not found')
    with tempfile.TemporaryDirectory(prefix='region-ocr-build-', dir=output) as temporary:
        work = Path(temporary)
        payload = work / 'app.zip'
        with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for file in sorted(files):
                name = file.relative_to(ROOT).as_posix()
                if name.startswith('installer/notices/'):
                    name = 'THIRD-PARTY-NOTICES/' + file.name
                archive.write(file, name)
        notices = (ROOT / 'LICENSES.md').read_text('utf-8')
        for file in sorted((HERE / 'notices').glob('*.txt')):
            notices += '\n\n' + '=' * 64 + '\n' + file.name + '\n\n' + file.read_text('utf-8')
        (work / 'notices.txt').write_text(notices, encoding='utf-8')
        config = ('<?xml version="1.0"?><configuration><startup><supportedRuntime version="v4.0" '
                  'sku=".NETFramework,Version=v4.8"/></startup><runtime><AppContextSwitchOverrides '
                  'value="Switch.System.IO.UseLegacyPathHandling=false;Switch.System.IO.BlockLongPaths=false"/>'
                  '</runtime></configuration>')
        (work / 'app.config').write_text(config, encoding='utf-8')
        (work / 'installer.manifest').write_text('''<?xml version="1.0" encoding="utf-8"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity version="1.8.6.0" name="EyesHead.RegionOCR.Setup"/>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
    <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
  </requestedPrivileges></security></trustInfo>
  <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
    <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
    <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true</dpiAware>
  </windowsSettings></application>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application>
    <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>
  </application></compatibility>
</assembly>''', encoding='utf-8')
        exe = output / f'REGION-OCR-{VERSION}-Setup.exe'
        args = [str(compiler), '/nologo', '/target:winexe', '/platform:anycpu', '/optimize+', '/utf8output',
                '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
                '/reference:System.Web.Extensions.dll', '/reference:System.IO.Compression.dll',
                f'/win32manifest:{work / "installer.manifest"}',
                f'/win32icon:{ROOT / "local_web/assets/app-icon.ico"}', f'/out:{exe}',
                f'/resource:{payload},app.zip', f'/resource:{HERE / "install-plan.json"},plan.json',
                f'/resource:{work / "notices.txt"},notices.txt', f'/resource:{work / "app.config"},app.config',
                f'/resource:{ROOT / "local_web/assets/app-icon.ico"},icon.ico', str(HERE / 'Setup.cs')]
        subprocess.run(args, check=True)
        report = work / 'verification.json'
        subprocess.run([str(exe), '--verify', str(report)], check=True, timeout=30)
        verified = json.loads(report.read_text('utf-8'))
        print('Embedded payload verified:', verified)
    assets = [exe]
    sums = ''.join(hashlib.sha256(file.read_bytes()).hexdigest() + '  ' + file.name + '\n' for file in assets)
    (output / 'SHA256SUMS').write_text(sums, encoding='ascii')
    print('Installer:', exe, exe.stat().st_size, 'bytes')
    return exe


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT.parent / 'releases' / ('v' + VERSION))
    build(parser.parse_args().output)
