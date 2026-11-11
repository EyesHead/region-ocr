using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("REGION OCR — установка")]
[assembly: AssemblyProduct("REGION OCR")]
[assembly: AssemblyVersion("1.8.6.0")]
[assembly: AssemblyFileVersion("1.8.6.0")]

internal static class Setup
{
    internal const string Product = "REGION OCR";
    internal const string Version = "1.8.6";
    internal const string Marker = ".region-ocr-install.json";
    internal const string RegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\RegionOCR";
    internal static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 4 * 1024 * 1024 };
    internal static string DefaultFolder { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", Product); } }
    internal static string UserData { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "RegionDocumentConverter"); } }

    [STAThread]
    static int Main(string[] args)
    {
        ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try
        {
            if (args.Length > 0 && args[0] == "--uninstall") return Uninstall();
            if (args.Length == 3 && args[0] == "--remove") return Remove(args[1], Int32.Parse(args[2]));
            if ((args.Length == 4 || args.Length == 8) && args[0] == "--update" && args[2] == "--wait-pid")
            {
                int parent;
                if (!Int32.TryParse(args[3], out parent) || parent <= 0) throw new ArgumentException("Некорректный процесс приложения");
                string restart = "";
                if (args.Length == 8)
                {
                    if (args[4] != "--data-dir" || args[6] != "--output-dir" || !Path.IsPathRooted(args[5]) || !Path.IsPathRooted(args[7]))
                        throw new ArgumentException("Некорректные папки данных приложения");
                    restart = "--data-dir " + Quote(Path.GetFullPath(args[5])) + " --output-dir " + Quote(Path.GetFullPath(args[7]));
                }
                using (var updater = new UpdateWindow(args[1], parent, restart)) Application.Run(updater);
                return 0;
            }
            if (args.Length == 2 && args[0] == "--verify")
            {
                string report = ValidatePayload();
                File.WriteAllText(args[1], report, new UTF8Encoding(false));
                return 0;
            }
            string existing = ExistingInstallation();
            if (existing != null)
            {
                if (MessageBox.Show("Найдена установленная копия REGION OCR:\n" + existing + "\n\nЗакройте конвертер перед продолжением. Обновить эту копию с сохранением документов, настроек и компонентов?\n\n«Нет» — отменить обновление.", Product, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return 0;
                using (var updater = new UpdateWindow(existing, 0)) Application.Run(updater);
                return 0;
            }
            using (var wizard = new Wizard()) Application.Run(wizard);
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, Product, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    internal static byte[] Resource(string name)
    {
        using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
        {
            if (stream == null) throw new InvalidDataException("Отсутствует ресурс: " + name);
            using (var output = new MemoryStream()) { stream.CopyTo(output); return output.ToArray(); }
        }
    }

    internal static Dictionary<string, object> Plan()
    {
        return Json.Deserialize<Dictionary<string, object>>(Encoding.UTF8.GetString(Resource("plan.json")));
    }

    internal static string Digest(string file)
    {
        using (var sha = SHA256.Create()) using (var stream = File.OpenRead(file))
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    internal static string SafePath(string folder, string name)
    {
        string root = Path.GetFullPath(folder).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        string result = Path.GetFullPath(Path.Combine(root, name));
        if (!result.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Недопустимый путь: " + name);
        return result;
    }

    internal static void Extract(Stream input, string folder)
    {
        using (var zip = new ZipArchive(input, ZipArchiveMode.Read))
            foreach (var entry in zip.Entries)
            {
                string target = SafePath(folder, entry.FullName);
                if (entry.Name.Length == 0) { Directory.CreateDirectory(target); continue; }
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                using (var source = entry.Open()) using (var output = File.Create(target)) source.CopyTo(output);
            }
    }

    internal static void ConfigurePython(string folder)
    {
        File.WriteAllText(Path.Combine(folder, "runtime", "python312._pth"),
            "python312.zip\n.\nLib\nLib\\site-packages\nDLLs\n..\n..\\local_web\nimport site\n", Encoding.ASCII);
    }

    static string ValidatePayload()
    {
        var plan = Plan();
        if ((string)plan["version"] != Version) throw new InvalidDataException("Версия плана не совпадает");
        int files = 0;
        using (var memory = new MemoryStream(Resource("app.zip")))
        using (var zip = new ZipArchive(memory, ZipArchiveMode.Read))
            foreach (var entry in zip.Entries)
            {
                SafePath(DefaultFolder, entry.FullName);
                using (var source = entry.Open()) { byte[] buffer = new byte[8192]; while (source.Read(buffer, 0, buffer.Length) > 0) { } }
                files++;
            }
        return Json.Serialize(new { verified = true, version = Version, payload_files = files, packages = ((ICollection)plan["packages"]).Count });
    }

    internal static string Quote(string text)
    {
        var result = new StringBuilder("\""); int slashes = 0;
        foreach (char value in text)
        {
            if (value == '\\') { slashes++; continue; }
            result.Append('\\', value == '"' ? slashes * 2 + 1 : slashes); slashes = 0; result.Append(value);
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }

    internal static void Shortcut(string name, string target, string arguments)
    {
        string folder = Path.GetDirectoryName(name);
        Directory.CreateDirectory(folder);
        Type type = Type.GetTypeFromProgID("WScript.Shell");
        object shell = Activator.CreateInstance(type);
        object link = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { name });
        Type item = link.GetType();
        item.InvokeMember("TargetPath", BindingFlags.SetProperty, null, link, new object[] { target });
        item.InvokeMember("Arguments", BindingFlags.SetProperty, null, link, new object[] { arguments });
        item.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link, new object[] { Path.GetDirectoryName(target) });
        item.InvokeMember("IconLocation", BindingFlags.SetProperty, null, link, new object[] { target + ",0" });
        item.InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
        Marshal.FinalReleaseComObject(link); Marshal.FinalReleaseComObject(shell);
    }

    internal static void Register(string folder, bool desktop, string profile)
    {
        string launcher = Path.Combine(folder, "DocumentConverter.exe");
        string uninstall = Path.Combine(folder, "Uninstall.exe");
        File.WriteAllText(Path.Combine(folder, Marker), Json.Serialize(new { product = Product, version = Version, profile = profile, folder = folder }), new UTF8Encoding(false));
        File.Copy(Assembly.GetExecutingAssembly().Location, uninstall, true);
        string programs = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), Product);
        Shortcut(Path.Combine(programs, Product + ".lnk"), launcher, "");
        Shortcut(Path.Combine(programs, "Удалить REGION OCR.lnk"), uninstall, "--uninstall");
        if (desktop) Shortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), Product + ".lnk"), launcher, "");
        using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RegistryPath))
        {
            key.SetValue("DisplayName", Product);
            key.SetValue("DisplayVersion", Version);
            key.SetValue("Publisher", "EyesHead");
            key.SetValue("InstallLocation", folder);
            key.SetValue("DisplayIcon", launcher);
            key.SetValue("UninstallString", Quote(uninstall) + " --uninstall");
            key.SetValue("URLInfoAbout", "https://github.com/EyesHead/region-ocr");
            key.SetValue("InstallDate", DateTime.Now.ToString("yyyyMMdd"));
            key.SetValue("NoModify", 1, RegistryValueKind.DWord);
            key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
        }
    }

    static int Uninstall()
    {
        string folder = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        ValidateInstalledFolder(folder);
        if (MessageBox.Show("Удалить REGION OCR и загруженные компоненты?\n\nИстория, результаты и настройки в " + UserData + " сохранятся.\n\nЗакройте конвертер перед удалением.", Product, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return 0;
        string helper = Path.Combine(Path.GetTempPath(), "RegionOCR-remove-" + Guid.NewGuid().ToString("N") + ".exe");
        File.Copy(Assembly.GetExecutingAssembly().Location, helper);
        File.WriteAllText(helper + ".config", Encoding.UTF8.GetString(Resource("app.config")), new UTF8Encoding(false));
        Process.Start(new ProcessStartInfo(helper, "--remove " + Quote(folder) + " " + Process.GetCurrentProcess().Id) { UseShellExecute = false });
        return 0;
    }

    internal static void ValidateInstalledFolder(string folder)
    {
        string full = Path.GetFullPath(folder).TrimEnd(Path.DirectorySeparatorChar);
        if (full == Path.GetPathRoot(full).TrimEnd(Path.DirectorySeparatorChar) || !File.Exists(Path.Combine(full, Marker)))
            throw new InvalidDataException("Папка не является установкой REGION OCR");
        var marker = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(full, Marker)));
        if ((string)marker["product"] != Product || !String.Equals((string)marker["folder"], full, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Не совпадает маркер установки");
        if ((File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0) throw new IOException("Папка установки является ссылкой");
    }

    static string ExistingInstallation()
    {
        string folder = null;
        using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath))
            if (key != null) folder = Convert.ToString(key.GetValue("InstallLocation"));
        if (String.IsNullOrEmpty(folder) && File.Exists(Path.Combine(DefaultFolder, Marker))) folder = DefaultFolder;
        if (String.IsNullOrEmpty(folder) || !File.Exists(Path.Combine(folder, Marker))) return null;
        ValidateInstalledFolder(folder);
        return Path.GetFullPath(folder).TrimEnd(Path.DirectorySeparatorChar);
    }

    static void EnsureApplicationClosed(string folder)
    {
        // The desktop host may use an explicitly configured Python outside the install folder.
        // Its existing single-instance file stays open for the entire window lifetime.
        string instanceLock = Path.Combine(UserData, "data", "desktop.lock");
        if (File.Exists(instanceLock))
        {
            try { using (var probe = new FileStream(instanceLock, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { } }
            catch (IOException) { throw new IOException("Конвертер ещё работает. Закройте его окно и повторите обновление. Установленные файлы сохранены."); }
        }
        foreach (var process in Process.GetProcesses())
            using (process)
            {
                string name;
                try { name = process.ProcessName; }
                catch (InvalidOperationException) { continue; }
                if (name != "python" && name != "pythonw" && name != "DocumentConverter" && name != "llama-server") continue;
                string executable;
                try { executable = process.MainModule.FileName; }
                catch (System.ComponentModel.Win32Exception) { continue; }
                catch (InvalidOperationException) { continue; }
                string prefix = folder + Path.DirectorySeparatorChar;
                if (executable.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                    throw new IOException("Конвертер или его обработчик ещё работает. Закройте приложение и повторите обновление. Документы и установленные файлы сохранены.");
            }
    }

    internal static void UpdateInstallation(string folder, int parent, Action<string> status, string restartArguments = "")
    {
        folder = Path.GetFullPath(folder).TrimEnd(Path.DirectorySeparatorChar);
        ValidateInstalledFolder(folder);
        ValidatePayload();
        var marker = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(folder, Marker)));
        System.Version oldVersion, newVersion = new System.Version(Version);
        if (!System.Version.TryParse(Convert.ToString(marker["version"]), out oldVersion) || oldVersion >= newVersion)
            throw new InvalidDataException("Версия обновления должна быть новее установленной");
        string profile = Convert.ToString(marker["profile"]);
        if (profile != "cpu" && profile != "cu128") throw new InvalidDataException("Неизвестный профиль установки");
        // Source-only updates reuse the exact installed dependency/model plan.
        // A future dependency change requires the full installer rather than silently mixing ABIs.
        var oldPlan = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(folder, "installer", "install-plan.json")));
        var newPlan = Plan(); oldPlan.Remove("version"); newPlan.Remove("version");
        if (Json.Serialize(oldPlan) != Json.Serialize(newPlan))
            throw new InvalidDataException("В этой версии изменились компоненты. Установите новую версию через полный установщик GitHub; текущая установка сохранена.");
        status("Ожидание закрытия конвертера…");
        try
        {
            if (parent > 0)
                using (var process = Process.GetProcessById(parent))
                    if (!process.WaitForExit(120000)) throw new IOException("Конвертер ещё работает. Закройте его и повторите обновление.");
        }
        catch (ArgumentException) { }
        ValidateInstalledFolder(folder);
        EnsureApplicationClosed(folder);
        var changes = new Dictionary<string, byte[]>(StringComparer.OrdinalIgnoreCase);
        using (var memory = new MemoryStream(Resource("app.zip")))
        using (var archive = new ZipArchive(memory, ZipArchiveMode.Read))
            foreach (var entry in archive.Entries)
            {
                if (entry.Name.Length == 0) continue;
                string name = entry.FullName.Replace('\\', '/');
                string first = name.Split('/')[0].ToLowerInvariant();
                if (first == "runtime" || first == "models" || first == ".runtime" || first == ".git"
                    || first == "ocr_output" || first == "output" || String.Equals(name, Marker, StringComparison.OrdinalIgnoreCase)
                    || name.StartsWith("local_web/data/", StringComparison.OrdinalIgnoreCase)
                    || name.StartsWith("local_web/desktop_runtime/", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Пакет обновления содержит пользовательские данные или компоненты: " + name);
                string target = SafePath(folder, name);
                ValidateUpdatePath(folder, target);
                using (var input = entry.Open()) using (var data = new MemoryStream())
                { input.CopyTo(data); changes.Add(target, data.ToArray()); }
            }
        changes[Path.Combine(folder, "Uninstall.exe")] = File.ReadAllBytes(Assembly.GetExecutingAssembly().Location);
        marker["version"] = Version;
        changes[Path.Combine(folder, Marker)] = new UTF8Encoding(false).GetBytes(Json.Serialize(marker));
        // Keep rollback bytes in memory: never copy user documents or create backup folders.
        var before = new Dictionary<string, byte[]>(StringComparer.OrdinalIgnoreCase);
        var written = new List<string>();
        foreach (string file in changes.Keys)
        {
            ValidateUpdatePath(folder, file);
            before.Add(file, File.Exists(file) ? File.ReadAllBytes(file) : null);
        }
        status("Обновление файлов приложения…");
        try
        {
            foreach (var change in changes)
            {
                ValidateUpdatePath(folder, change.Key);
                Directory.CreateDirectory(Path.GetDirectoryName(change.Key));
                written.Add(change.Key);
                File.WriteAllBytes(change.Key, change.Value);
            }
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath, true))
                if (key != null && String.Equals(Convert.ToString(key.GetValue("InstallLocation")), folder, StringComparison.OrdinalIgnoreCase))
                    key.SetValue("DisplayVersion", Version);
        }
        catch
        {
            written.Reverse();
            foreach (string file in written)
                if (before[file] == null) File.Delete(file); else File.WriteAllBytes(file, before[file]);
            throw;
        }
        status("Обновление установлено. Запуск конвертера…");
        Process.Start(new ProcessStartInfo(Path.Combine(folder, "DocumentConverter.exe"), restartArguments) { WorkingDirectory = folder, UseShellExecute = false, CreateNoWindow = true });
    }

    static void ValidateUpdatePath(string folder, string target)
    {
        string path = target;
        while (!String.Equals(path, folder, StringComparison.OrdinalIgnoreCase))
        {
            if ((File.Exists(path) || Directory.Exists(path)) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("Обновление не может записывать через ссылку: " + path);
            path = Path.GetDirectoryName(path);
            if (String.IsNullOrEmpty(path)) throw new IOException("Путь обновления вышел за папку приложения");
        }
    }

    internal static void CleanupUpdateDownload()
    {
        string self = Assembly.GetExecutingAssembly().Location;
        string expected = Path.Combine(UserData, "updates").TrimEnd(Path.DirectorySeparatorChar);
        if (!String.Equals(Path.GetDirectoryName(self), expected, StringComparison.OrdinalIgnoreCase)) return;
        string script = "Wait-Process -Id " + Process.GetCurrentProcess().Id + " -ErrorAction SilentlyContinue; Remove-Item -LiteralPath '" + self.Replace("'", "''") + "' -Force -ErrorAction SilentlyContinue; if ((Get-ChildItem -LiteralPath '" + expected.Replace("'", "''") + "' -Force | Measure-Object).Count -eq 0) { Remove-Item -LiteralPath '" + expected.Replace("'", "''") + "' -ErrorAction SilentlyContinue }";
        Process.Start(new ProcessStartInfo("powershell.exe", "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand " + Convert.ToBase64String(Encoding.Unicode.GetBytes(script))) { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden });
    }

    static int Remove(string folder, int parent)
    {
        try { using (var process = Process.GetProcessById(parent)) process.WaitForExit(15000); } catch (ArgumentException) { }
        ValidateInstalledFolder(folder);
        // Refuse to traverse links inserted after installation.
        DeleteTree(folder);
        Unregister(folder);
        MessageBox.Show("REGION OCR удалён. История и настройки сохранены.", Product);
        string self = Assembly.GetExecutingAssembly().Location;
        // A separate system PowerShell process deletes only these two known temp files after this helper exits.
        string script = "Wait-Process -Id " + Process.GetCurrentProcess().Id + " -ErrorAction SilentlyContinue; Remove-Item -LiteralPath '" + self.Replace("'", "''") + "','" + (self + ".config").Replace("'", "''") + "' -Force -ErrorAction SilentlyContinue";
        Process.Start(new ProcessStartInfo("powershell.exe", "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand " + Convert.ToBase64String(Encoding.Unicode.GetBytes(script))) { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden });
        return 0;
    }

    internal static void Unregister(string folder)
    {
        string programs = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), Product);
        DeleteShortcut(Path.Combine(programs, Product + ".lnk"), Path.Combine(folder, "DocumentConverter.exe"));
        DeleteShortcut(Path.Combine(programs, "Удалить REGION OCR.lnk"), Path.Combine(folder, "Uninstall.exe"));
        if (Directory.Exists(programs) && Directory.GetFileSystemEntries(programs).Length == 0) Directory.Delete(programs);
        DeleteShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), Product + ".lnk"), Path.Combine(folder, "DocumentConverter.exe"));
        using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath))
        {
            bool ours = key != null && String.Equals(Convert.ToString(key.GetValue("InstallLocation")), folder, StringComparison.OrdinalIgnoreCase);
            if (ours) Registry.CurrentUser.DeleteSubKeyTree(RegistryPath, false);
        }
    }

    static void DeleteShortcut(string name, string target)
    {
        if (!File.Exists(name)) return;
        Type type = Type.GetTypeFromProgID("WScript.Shell"); object shell = Activator.CreateInstance(type);
        object link = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { name });
        string actual = Convert.ToString(link.GetType().InvokeMember("TargetPath", BindingFlags.GetProperty, null, link, null));
        Marshal.FinalReleaseComObject(link); Marshal.FinalReleaseComObject(shell);
        if (String.Equals(actual, target, StringComparison.OrdinalIgnoreCase)) File.Delete(name);
    }

    internal static void DeleteTree(string folder)
    {
        foreach (string child in Directory.GetDirectories(folder))
        {
            if ((File.GetAttributes(child) & FileAttributes.ReparsePoint) != 0) Directory.Delete(child);
            else DeleteTree(child);
        }
        foreach (string file in Directory.GetFiles(folder)) { File.SetAttributes(file, FileAttributes.Normal); File.Delete(file); }
        Directory.Delete(folder);
    }
}

internal sealed class UpdateWindow : Form
{
    readonly string folder;
    readonly int parent;
    readonly string restartArguments;
    readonly Label status = new Label();
    bool working = true;

    internal UpdateWindow(string folder, int parent, string restartArguments = "")
    {
        this.folder = folder; this.parent = parent; this.restartArguments = restartArguments;
        Text = "Обновление REGION OCR " + Setup.Version;
        ClientSize = new Size(560, 140); FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen; MaximizeBox = false; MinimizeBox = false;
        Font = new Font("Segoe UI", 10);
        status.SetBounds(24, 25, 510, 90); status.Text = "Подготовка обновления…"; Controls.Add(status);
        FormClosing += delegate(object sender, FormClosingEventArgs e) { if (working) e.Cancel = true; };
        Shown += async delegate
        {
            try { await Task.Run(() => Setup.UpdateInstallation(folder, parent, text => BeginInvoke(new Action(() => status.Text = text)), restartArguments)); }
            catch (Exception error) { MessageBox.Show(this, error.Message, "Обновление не завершено", MessageBoxButtons.OK, MessageBoxIcon.Error); }
            finally { working = false; Setup.CleanupUpdateDownload(); Close(); }
        };
    }
}

internal sealed class Wizard : Form
{
    TextBox destination = new TextBox();
    CheckBox desktop = new CheckBox();
    CheckBox cuda = new CheckBox();
    CheckBox launch = new CheckBox();
    Button browse = new Button();
    Button install = new Button();
    Button cancel = new Button();
    Label message = new Label();
    ProgressBar progress = new ProgressBar();
    TextBox details = new TextBox();
    CancellationTokenSource stop;
    Process running;
    bool busy, complete;
    string installedFolder;

    internal Wizard()
    {
        Text = "Установка REGION OCR " + Setup.Version;
        ClientSize = new Size(660, 495); MinimumSize = MaximumSize = Size;
        Font = new Font("Segoe UI", 9); StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false;
        using (var stream = new MemoryStream(Setup.Resource("icon.ico"))) Icon = new Icon(stream);
        var title = new Label { Text = "REGION OCR", Font = new Font("Segoe UI", 20, FontStyle.Bold), AutoSize = true, Location = new Point(26, 20) };
        Controls.Add(title);
        Controls.Add(new Label { Text = "Конвертер PDF и Office в HTML · Windows x64", AutoSize = true, Location = new Point(29, 64) });
        Controls.Add(new Label { Text = "Папка установки", AutoSize = true, Location = new Point(28, 105) });
        destination.SetBounds(28, 128, 502, 28); destination.Text = Setup.DefaultFolder; Controls.Add(destination);
        browse.SetBounds(541, 127, 90, 28); browse.Text = "Обзор…";
        browse.Click += delegate { using (var dialog = new FolderBrowserDialog { SelectedPath = destination.Text, Description = "Папка для REGION OCR" }) if (dialog.ShowDialog(this) == DialogResult.OK) destination.Text = dialog.SelectedPath; }; Controls.Add(browse);
        cuda.SetBounds(28, 172, 600, 24); cuda.Text = "Ускорение NVIDIA CUDA (для совместимой видеокарты)";
        cuda.Checked = File.Exists(Path.Combine(Environment.SystemDirectory, "nvcuda.dll")); Controls.Add(cuda);
        desktop.SetBounds(28, 202, 600, 24); desktop.Text = "Создать ярлык на рабочем столе"; desktop.Checked = true; Controls.Add(desktop);
        var note = new Label { Text = "Python, библиотеки и модели установятся автоматически.\nНужен интернет: около 3 ГБ (CPU) или 6 ГБ (CUDA).\nСвободное место: от 12 ГБ. Права администратора не требуются.", Location = new Point(28, 238), Size = new Size(600, 65) }; Controls.Add(note);
        var licenses = new LinkLabel { Text = "Лицензии компонентов", AutoSize = true, Location = new Point(28, 309) };
        licenses.LinkClicked += delegate { ShowLicenses(); }; Controls.Add(licenses);
        message.SetBounds(28, 345, 604, 38); message.Text = "Всё готово к установке."; Controls.Add(message);
        progress.SetBounds(28, 385, 604, 17); Controls.Add(progress);
        launch.SetBounds(28, 413, 380, 24); launch.Text = "Запустить REGION OCR"; launch.Checked = true; launch.Visible = false; Controls.Add(launch);
        details.SetBounds(28, 106, 604, 225); details.Multiline = true; details.ReadOnly = true; details.ScrollBars = ScrollBars.Vertical; details.Visible = false; Controls.Add(details);
        install.SetBounds(414, 447, 112, 31); install.Text = "Установить"; install.Click += async delegate { if (complete) { if (launch.Checked) Process.Start(Path.Combine(installedFolder, "DocumentConverter.exe")); Close(); } else await Install(); }; Controls.Add(install);
        cancel.SetBounds(538, 447, 94, 31); cancel.Text = "Отмена"; cancel.Click += delegate { if (busy) Cancel(); else Close(); }; Controls.Add(cancel);
        FormClosing += delegate(object sender, FormClosingEventArgs e) { if (busy) { e.Cancel = true; Cancel(); } };
    }

    void ShowLicenses()
    {
        using (var form = new Form { Text = "Лицензии REGION OCR", Size = new Size(820, 620), StartPosition = FormStartPosition.CenterParent })
        {
            form.Controls.Add(new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both, WordWrap = false, Dock = DockStyle.Fill, Font = new Font("Consolas", 9), Text = Encoding.UTF8.GetString(Setup.Resource("notices.txt")).Replace("\n", "\r\n") });
            form.ShowDialog(this);
        }
    }

    void Status(int percent, string text)
    {
        if (InvokeRequired) { BeginInvoke(new Action<int, string>(Status), percent, text); return; }
        progress.Value = Math.Max(0, Math.Min(100, percent)); message.Text = text;
        if (details.Text.Length > 60000) details.Text = details.Text.Substring(details.Text.Length - 30000);
        details.AppendText(text + Environment.NewLine);
    }

    void Cancel()
    {
        if (stop == null || stop.IsCancellationRequested) return;
        stop.Cancel(); cancel.Enabled = false; message.Text = "Отмена установки и очистка временных файлов…";
        // Kill only the process tree created by this installer.
        try
        {
            var current = running;
            if (current != null && !current.HasExited)
                Task.Run(delegate {
                    using (var killer = Process.Start(new ProcessStartInfo("taskkill.exe", "/PID " + current.Id + " /T /F") { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden }))
                        if (killer != null) killer.WaitForExit(15000);
                });
        }
        catch (InvalidOperationException) { }
    }

    void DownloadStatus(string target, string text)
    {
        Status(3, text);
        try { File.AppendAllText(Path.Combine(Path.GetDirectoryName(target), "installation.log"), DateTime.UtcNow.ToString("o") + " " + text + Environment.NewLine, new UTF8Encoding(false)); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    void Download(Dictionary<string, object> item, string target, CancellationToken token)
    {
        long expected = Convert.ToInt64(item["bytes"]);
        string name = (string)item["filename"], host = new Uri((string)item["url"]).Host;
        for (int attempt = 0; attempt < 8; attempt++)
        {
            token.ThrowIfCancellationRequested();
            try
            {
                long offset = File.Exists(target) ? new FileInfo(target).Length : 0;
                if (offset >= expected)
                {
                    if (offset == expected && Setup.Digest(target) == (string)item["sha256"]) return;
                    File.Delete(target); offset = 0;
                }
                DownloadStatus(target, "Загрузка " + name + " с " + host + ": попытка " + (attempt + 1) + "/8, сохранено " + offset + " байт.");
                var request = (HttpWebRequest)WebRequest.Create((string)item["url"]);
                request.UserAgent = "REGION-OCR-Setup/1.8.5"; request.Timeout = request.ReadWriteTimeout = 180000;
                if (offset > 0) request.AddRange(offset);
                using (token.Register(request.Abort))
                using (var response = (HttpWebResponse)request.GetResponse())
                {
                    long responseBytes = expected;
                    if (response.StatusCode == HttpStatusCode.PartialContent)
                    {
                        var range = Regex.Match(response.Headers["Content-Range"] ?? "", @"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$");
                        long start, end, total;
                        if (!range.Success || !Int64.TryParse(range.Groups[1].Value, out start)
                            || !Int64.TryParse(range.Groups[2].Value, out end) || !Int64.TryParse(range.Groups[3].Value, out total)
                            || start != offset || total != expected || end < start || end >= total)
                            throw new InvalidDataException("Недопустимый Content-Range для " + name);
                        responseBytes = end - start + 1;
                        if (response.ContentLength >= 0 && response.ContentLength != responseBytes)
                            throw new InvalidDataException("Неверная длина диапазона для " + name);
                    }
                    else if (response.StatusCode == HttpStatusCode.OK)
                    {
                        // A server may ignore Range. Replace the partial file instead of appending a full response.
                        offset = 0;
                        if (response.ContentLength >= 0 && response.ContentLength != expected)
                            throw new InvalidDataException("Неверный размер ответа для " + name);
                    }
                    else throw new InvalidDataException("Неожиданный HTTP-ответ для " + name);
                    using (var input = response.GetResponseStream())
                    using (var output = new FileStream(target, offset > 0 ? FileMode.Append : FileMode.Create, FileAccess.Write))
                    {
                        byte[] buffer = new byte[64 * 1024]; int count; long received = 0;
                        while ((count = input.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            token.ThrowIfCancellationRequested();
                            received += count;
                            if (received > responseBytes) throw new InvalidDataException("Ответ превышает ожидаемый размер для " + name);
                            output.Write(buffer, 0, count);
                        }
                        if (received != responseBytes) throw new IOException("Загрузка прервана: " + name);
                    }
                }
                if (new FileInfo(target).Length != expected) throw new IOException("Загружена только часть " + name);
                if (Setup.Digest(target) != (string)item["sha256"])
                    throw new InvalidDataException("Не совпадает SHA256: " + item["filename"]);
                token.ThrowIfCancellationRequested();
                DownloadStatus(target, "Размер и SHA256 проверены: " + name);
                return;
            }
            catch (Exception error)
            {
                token.ThrowIfCancellationRequested();
                // Interrupted reads retain verified-by-position bytes; invalid responses and hashes start over.
                bool restart = error is InvalidDataException;
                string reason = error.GetType().Name;
                var network = error as WebException;
                if (network != null)
                {
                    reason += "/" + network.Status;
                    var failedResponse = network.Response as HttpWebResponse;
                    if (failedResponse != null && failedResponse.StatusCode == HttpStatusCode.RequestedRangeNotSatisfiable) restart = true;
                    if (network.Response != null) network.Response.Close();
                }
                if (restart && File.Exists(target)) File.Delete(target);
                DownloadStatus(target, "Не удалось загрузить " + name + ", попытка " + (attempt + 1) + "/8: " + reason + ". Повторная попытка продолжит загрузку.");
                if (attempt == 7) throw new IOException("Не удалось загрузить " + name + " с " + host + " после 8 попыток. Проверьте интернет, VPN или прокси и повторите установку. Подробности в installation.log.", error);
                if (token.WaitHandle.WaitOne(1000 * (attempt + 1))) token.ThrowIfCancellationRequested();
            }
        }
    }

    async Task Install()
    {
        if (!Environment.Is64BitOperatingSystem) { MessageBox.Show("Требуется 64-разрядная Windows."); return; }
        string target;
        try
        {
            target = Path.GetFullPath(destination.Text).TrimEnd(Path.DirectorySeparatorChar);
            if (target == Path.GetPathRoot(target).TrimEnd(Path.DirectorySeparatorChar)) throw new IOException("Выберите отдельную папку приложения.");
            if (Directory.Exists(target) && Directory.GetFileSystemEntries(target).Length > 0) throw new IOException("Папка уже содержит файлы. Выберите пустую папку или удалите прежнюю установку через Windows.");
            using (var key = Registry.CurrentUser.OpenSubKey(Setup.RegistryPath))
                if (key != null && Directory.Exists(Convert.ToString(key.GetValue("InstallLocation")))) throw new IOException("REGION OCR уже установлен. Сначала удалите его через «Установленные приложения». История сохранится.");
            var disk = new DriveInfo(Path.GetPathRoot(target));
            if (disk.AvailableFreeSpace < 12L * 1024 * 1024 * 1024) throw new IOException("Недостаточно места: требуется от 12 ГБ.");
            if (target.Length > 130) throw new IOException("Выберите более короткий путь установки (до 130 символов).");
        }
        catch (Exception error) { MessageBox.Show(error.Message, Setup.Product); return; }
        string profile = cuda.Checked ? "cu128" : "cpu";
        bool wantDesktop = desktop.Checked;
        string staging = Path.Combine(Path.GetDirectoryName(target), ".region-ocr-install-" + Guid.NewGuid().ToString("N").Substring(0, 8));
        busy = true; stop = new CancellationTokenSource(); install.Enabled = false;
        destination.Visible = browse.Visible = cuda.Visible = desktop.Visible = false;
        details.Visible = true; details.BringToFront();
        bool moved = false;
        try
        {
            await Task.Run(delegate
            {
                Directory.CreateDirectory(staging);
                Status(1, "Распаковка приложения");
                using (var stream = new MemoryStream(Setup.Resource("app.zip"))) Setup.Extract(stream, staging);
                var plan = Setup.Plan();
                Status(3, "Загрузка Python 3.12.10");
                string archive = Path.Combine(staging, "python.zip");
                Download((Dictionary<string, object>)plan["python"], archive, stop.Token);
                using (var stream = File.OpenRead(archive)) Setup.Extract(stream, Path.Combine(staging, "runtime"));
                File.Delete(archive);
                Setup.ConfigurePython(staging);
                string python = Path.Combine(staging, "runtime", "python.exe");
                var info = new ProcessStartInfo(python, "-s -u " + Setup.Quote(Path.Combine(staging, "installer", "bootstrap.py")) + " " + Setup.Quote(staging) + " " + profile);
                info.UseShellExecute = false; info.CreateNoWindow = true; info.RedirectStandardOutput = true; info.RedirectStandardError = true;
                info.StandardOutputEncoding = info.StandardErrorEncoding = Encoding.UTF8; info.WorkingDirectory = staging;
                info.EnvironmentVariables["PYTHONUTF8"] = "1"; info.EnvironmentVariables["PYTHONNOUSERSITE"] = "1"; info.EnvironmentVariables.Remove("PYTHONPATH");
                var errors = new StringBuilder();
                using (var process = new Process { StartInfo = info })
                {
                    process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
                    {
                        if (String.IsNullOrWhiteSpace(e.Data)) return;
                        try
                        {
                            var line = Setup.Json.Deserialize<Dictionary<string, object>>(e.Data);
                            if (line.ContainsKey("error")) { lock (errors) errors.AppendLine((string)line["error"]); }
                            else Status(Convert.ToInt32(line["progress"]), (string)line["message"]);
                        }
                        catch (ArgumentException) { }
                    };
                    process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) lock (errors) errors.AppendLine(e.Data); };
                    stop.Token.ThrowIfCancellationRequested(); process.Start(); running = process;
                    process.BeginOutputReadLine(); process.BeginErrorReadLine(); process.WaitForExit(); running = null;
                    stop.Token.ThrowIfCancellationRequested();
                    if (process.ExitCode != 0) throw new IOException(errors.Length == 0 ? "Ошибка установки компонентов" : errors.ToString());
                }
                stop.Token.ThrowIfCancellationRequested();
                Status(97, "Создание ярлыков и регистрация в Windows");
                if (Directory.Exists(target)) Directory.Delete(target); // Already verified empty; never recursive.
                Directory.Move(staging, target); moved = true;
                Setup.Register(target, wantDesktop, profile);
            });
            installedFolder = target; complete = true;
            Status(100, "REGION OCR установлен. Можно запускать конвертер.");
            launch.Visible = true; install.Text = "Готово"; cancel.Visible = false;
        }
        catch (Exception error)
        {
            string log = Path.Combine(moved ? target : staging, "installation.log");
            if (File.Exists(log))
            {
                Directory.CreateDirectory(Setup.UserData);
                File.Copy(log, Path.Combine(Setup.UserData, "last-installation.log"), true);
            }
            Status(0, stop.IsCancellationRequested ? "Установка отменена. Временные файлы удалены." : "Установка не завершена. " + error.Message);
            if (!stop.IsCancellationRequested) MessageBox.Show(error.Message + "\n\nЖурнал: " + Path.Combine(Setup.UserData, "last-installation.log"), "Не удалось установить REGION OCR", MessageBoxButtons.OK, MessageBoxIcon.Error);
            install.Text = "Повторить"; cancel.Text = "Закрыть";
        }
        finally
        {
            if (Directory.Exists(staging))
            {
                for (int retry = 0; retry < 5; retry++)
                {
                    try { Setup.DeleteTree(staging); break; }
                    catch (Exception cleanupError)
                    {
                        if (retry == 4) { Status(0, "Не удалось полностью очистить " + staging + ": " + cleanupError.Message); break; }
                        Thread.Sleep(500);
                    }
                }
            }
            if (!complete && moved && Directory.Exists(target))
            {
                // Registration can fail after the atomic move. Retain this owned folder
                // for the user to inspect rather than recursively deleting an unverified path.
                Status(0, "Компоненты сохранены в " + target + ". Регистрация не завершена.");
            }
            busy = false; stop.Dispose(); stop = null; install.Enabled = true; cancel.Enabled = true;
        }
    }
}
