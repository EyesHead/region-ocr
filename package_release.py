"""Package a built portable installation into GitHub-sized release assets."""
from pathlib import Path
import hashlib
import io
import json
import os
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parent
VERSION = '1.8.0'
FOLDER = f'REGION-OCR-{VERSION}-win-x64'
PART_BYTES = 512 * 1024 * 1024
EXCLUDE_TOP = {'.git', '.cache', 'Input', 'Output', 'ocr_output', 'convert_docling_log'}


class PartsWriter:
    def __init__(self, destination):
        self.destination = destination
        self.total = self.size = 0
        self.parts = []
        self.stream = None
        self.digest = None

    def tell(self):
        return self.total

    def seek(self, *args):
        raise io.UnsupportedOperation('Sequential multipart archive')

    def flush(self):
        if self.stream:
            self.stream.flush()

    def finish_part(self):
        if self.stream is None:
            return
        self.stream.close()
        self.parts.append({'name': self.name, 'bytes': self.size,
                           'sha256': self.digest.hexdigest()})
        print('Finished', self.name, self.size, flush=True)
        self.stream = None

    def write(self, data):
        length = len(data)
        data = memoryview(data)
        while data:
            if self.stream is None:
                self.name = FOLDER + f'.zip.{len(self.parts) + 1:03d}'
                self.stream = (self.destination / self.name).open('wb')
                self.digest = hashlib.sha256()
                self.size = 0
            count = min(len(data), PART_BYTES - self.size)
            chunk = data[:count]
            self.stream.write(chunk)
            self.digest.update(chunk)
            self.size += count
            self.total += count
            data = data[count:]
            if self.size == PART_BYTES:
                self.finish_part()
        return length


SETUP_SOURCE = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Windows.Forms;
[assembly:System.Reflection.AssemblyTitle("REGION OCR Setup")]
[assembly:System.Reflection.AssemblyVersion("1.8.0.0")]
internal static class Setup {
    const string Folder = "@FOLDER@";
    const string BaseUrl = "https://github.com/EyesHead/region-ocr/releases/download/v1.8.0/";
    static readonly string[] Names = new string[] { @NAMES@ };
    static readonly string[] Hashes = new string[] { @HASHES@ };
    static string Digest(string file) {
        using (var sha = SHA256.Create()) using (var input = File.OpenRead(file))
            return BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
    }
    static bool Valid(string file, int index) {
        return File.Exists(file) && Digest(file) == Hashes[index];
    }
    static void Download(string url, string file) {
        var request = (HttpWebRequest)WebRequest.Create(url);
        request.UserAgent = "REGION-OCR-Setup/1.8.0";
        request.Timeout = 120000; request.ReadWriteTimeout = 120000;
        using (var response = request.GetResponse()) using (var input = response.GetResponseStream())
        using (var output = File.Create(file)) {
            var buffer = new byte[1024 * 1024]; long total = 0, next = 0; int count;
            while ((count = input.Read(buffer, 0, buffer.Length)) > 0) {
                output.Write(buffer, 0, count); total += count;
                if (total >= next) { Console.Write("\r  {0} MB", total / 1048576); next = total + 16 * 1048576; }
            }
        }
        Console.WriteLine();
    }
    [STAThread] static int Main(string[] args) {
        string work = null, staging = null;
        try {
            ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
            string here = AppDomain.CurrentDomain.BaseDirectory;
            if (Array.IndexOf(args, "--verify") >= 0) {
                for (int i = 0; i < Names.Length; i++) {
                    if (!Valid(Path.Combine(here, Names[i]), i)) throw new Exception("SHA256: " + Names[i]);
                    Console.WriteLine("OK " + Names[i]);
                }
                return 0;
            }
            Console.OutputEncoding = System.Text.Encoding.UTF8;
            Console.WriteLine("REGION OCR 1.8.0 — Python и модели входят в сборку.");
            string parent;
            using (var choose = new FolderBrowserDialog()) {
                choose.Description = "Выберите папку для распаковки REGION OCR";
                choose.SelectedPath = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                if (choose.ShowDialog() != DialogResult.OK) return 0;
                parent = Path.GetFullPath(choose.SelectedPath);
            }
            string target = Path.Combine(parent, Folder);
            if (Directory.Exists(target)) throw new Exception("Папка уже существует: " + target + ". Выберите другую папку.");
            work = Path.Combine(Path.GetTempPath(), "region-ocr-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(work);
            string archive = Path.Combine(work, "portable.zip");
            using (var output = File.Create(archive)) {
                for (int i = 0; i < Names.Length; i++) {
                    Console.WriteLine("Часть {0} из {1}: {2}", i + 1, Names.Length, Names[i]);
                    string part = Path.Combine(here, Names[i]);
                    if (!Valid(part, i)) {
                        part = Path.Combine(work, Names[i]);
                        Download(BaseUrl + Names[i], part);
                        if (!Valid(part, i)) throw new Exception("Контрольная сумма не совпадает: " + Names[i]);
                    }
                    using (var input = File.OpenRead(part)) input.CopyTo(output, 1024 * 1024);
                }
            }
            staging = Path.Combine(parent, ".region-ocr-install-" + Guid.NewGuid().ToString("N"));
            Console.WriteLine("Распаковка конвертера, Python и моделей...");
            ZipFile.ExtractToDirectory(archive, staging);
            string payload = Path.Combine(staging, Folder);
            if (!File.Exists(Path.Combine(payload, "DocumentConverter.exe"))) throw new Exception("В архиве отсутствует конвертер.");
            Directory.Move(payload, target);
            Directory.Delete(staging); staging = null;
            Console.WriteLine("Готово: " + target);
            Process.Start(new ProcessStartInfo(Path.Combine(target, "DocumentConverter.exe")) { WorkingDirectory = target, UseShellExecute = true });
            return 0;
        } catch (Exception error) {
            Console.Error.WriteLine(error.Message);
            if (Array.IndexOf(args, "--verify") < 0) MessageBox.Show(error.Message, "REGION OCR", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        } finally {
            if (work != null && Directory.Exists(work)) { try { Directory.Delete(work, true); } catch { } }
            if (staging != null && Directory.Exists(staging)) { try { Directory.Delete(staging, true); } catch { } }
        }
    }
}
'''


def main():
    output = ROOT.parent / 'releases' / ('v' + VERSION)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError('Release output already exists; refusing to overwrite it')
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for base, directories, files in os.walk(ROOT):
        relative = Path(base).relative_to(ROOT)
        directories[:] = [d for d in directories if d not in {'__pycache__', '.locks'}
                           and not (relative == Path('.') and d in EXCLUDE_TOP)
                           and not (relative == Path('local_web') and d in {'data', 'qa', 'tests'})]
        for name in files:
            if name.endswith(('.pyc', '.pyo', '.log', '.lock', '.tmp', '.incomplete')):
                continue
            paths.append(Path(base) / name)
    writer = PartsWriter(output)
    with zipfile.ZipFile(writer, 'w', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=1, allowZip64=True) as archive:
        for i, path in enumerate(sorted(paths), 1):
            archive.write(path, FOLDER + '/' + path.relative_to(ROOT).as_posix())
            if i % 1000 == 0:
                print('Files', i, '/', len(paths), flush=True)
        archive.writestr(FOLDER + '/Input/', b'')
        archive.writestr(FOLDER + '/Output/', b'')
    writer.finish_part()
    names = ', '.join('"' + p['name'] + '"' for p in writer.parts)
    hashes = ', '.join('"' + p['sha256'] + '"' for p in writer.parts)
    setup_source = SETUP_SOURCE.replace('@FOLDER@', FOLDER).replace('@NAMES@', names).replace('@HASHES@', hashes)
    source = output / 'setup.cs'
    source.write_text(setup_source, encoding='utf-8-sig')
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    setup = output / f'REGION-OCR-{VERSION}-Setup.exe'
    subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:anycpu', '/optimize+', '/utf8output',
                    '/reference:System.Windows.Forms.dll', '/reference:System.IO.Compression.FileSystem.dll',
                    '/out:' + str(setup), str(source)], check=True)
    source.unlink()
    with setup.open('rb') as stream:
        setup_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    assets = [dict(name=setup.name, bytes=setup.stat().st_size, sha256=setup_hash)] + writer.parts
    (output / 'SHA256SUMS').write_text(''.join(p['sha256'] + '  ' + p['name'] + '\n' for p in assets), encoding='utf-8')
    (output / 'release-manifest.json').write_text(json.dumps({'version': VERSION, 'folder': FOLDER,
        'archive_files': len(paths) + 2, 'assets': assets}, indent=2), encoding='utf-8')
    print(json.dumps({'assets': len(assets), 'compressed_bytes': writer.total,
                      'directory': str(output)}), flush=True)


if __name__ == '__main__':
    main()
