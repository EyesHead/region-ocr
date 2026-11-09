using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

// Portable installation launcher; the bundled Python takes priority over PATH.
internal static class DocumentConverterLauncher
{
    private const string AppMarker = "region-document-converter";
    private const int PreferredPort = 8765;
    private static string startupLog;

    [STAThread]
    private static int Main(string[] args)
    {
        bool checkOnly = Array.IndexOf(args, "--check") >= 0;
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string python = FindPython(root);
            string logs = Path.Combine(root, "local_web", "data");
            Directory.CreateDirectory(logs);
            startupLog = Path.Combine(logs, "desktop-launcher.log");
            if (checkOnly) { Log("CHECK OK " + python); return 0; }
            string server = Path.Combine(root, "local_web", "server.py");
            string bootstrap = "import os,runpy,sys; log=open(sys.argv.pop(1),'a',encoding='utf-8',buffering=1); sys.stdout=sys.stderr=log; sys.argv=sys.argv[1:]; sys.path.insert(0,os.path.dirname(os.path.dirname(sys.argv[0]))); from portable_runtime import configure_runtime; configure_runtime(); sys.path.insert(0,os.path.dirname(sys.argv[0])); runpy.run_path(sys.argv[0],run_name='__main__')";
            ProcessStartInfo info = new ProcessStartInfo(python);
            info.Arguments = "-s -u -c " + Quote(bootstrap) + " " + Quote(startupLog) + " " + Quote(server);
            bool desktop = Array.IndexOf(args, "--no-browser") < 0;
            if (desktop) info.Arguments += " --desktop";
            foreach (string arg in args) if (arg != "--no-browser") info.Arguments += " " + Quote(arg);
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.WindowStyle = desktop ? ProcessWindowStyle.Normal : ProcessWindowStyle.Hidden;
            info.EnvironmentVariables["PYTHONUTF8"] = "1";
            info.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
            info.EnvironmentVariables["DOCUMENT_CONVERTER_PYTHON"] = python;
            Process.Start(info);
            return 0;
        }
        catch (Exception error)
        {
            Log(error.ToString());
            if (!checkOnly) MessageBox.Show(error.Message, "Не удалось запустить конвертер", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static string FindPython(string root)
    {
        List<string> candidates = new List<string>();
        string configured = Environment.GetEnvironmentVariable("DOCUMENT_CONVERTER_PYTHON");
        if (String.IsNullOrWhiteSpace(configured)) configured = Environment.GetEnvironmentVariable("DOCLING_PYTHON");
        bool explicitSelection = !String.IsNullOrWhiteSpace(configured);
        if (explicitSelection)
        {
            if (!File.Exists(configured))
                throw new FileNotFoundException("Указанный в DOCUMENT_CONVERTER_PYTHON / DOCLING_PYTHON интерпретатор не найден: " + configured + ". Исправьте путь или удалите переменную для автоматического выбора.");
            Add(candidates, configured);
        }
        else
        {
            Add(candidates, Path.Combine(root, "runtime", "python.exe"));
            foreach (string folder in new string[] { root, Directory.GetParent(root).FullName })
            {
                Add(candidates, Path.Combine(folder, ".venv", "Scripts", "python.exe"));
                Add(candidates, Path.Combine(folder, "venv", "Scripts", "python.exe"));
            }
            foreach (string folder in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
            {
                if (String.IsNullOrWhiteSpace(folder)) continue;
                try { Add(candidates, Path.Combine(folder.Trim('"'), "python.exe")); }
                catch (ArgumentException) { }
            }
            string localPython = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Python");
            Add(candidates, Path.Combine(localPython, "Python312", "python.exe"));
            if (Directory.Exists(localPython))
            {
                string[] installations = Directory.GetDirectories(localPython, "Python3*");
                Array.Sort(installations, StringComparer.OrdinalIgnoreCase);
                Array.Reverse(installations);
                foreach (string folder in installations) Add(candidates, Path.Combine(folder, "python.exe"));
            }
        }
        foreach (string candidate in candidates)
        {
            if (!File.Exists(candidate)) continue;
            try
            {
                ProcessStartInfo info = new ProcessStartInfo(candidate, "-c " + Quote("import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 7)"));
                info.UseShellExecute = false;
                info.CreateNoWindow = true;
                info.RedirectStandardOutput = true;
                info.RedirectStandardError = true;
                using (Process check = Process.Start(info))
                {
                    if (check != null && check.WaitForExit(4000) && check.ExitCode == 0) return candidate;
                    if (check != null && !check.HasExited) check.Kill();
                }
            }
            catch (Exception error) { Log("Python candidate rejected: " + candidate + " | " + error.Message); }
        }
        throw new FileNotFoundException(explicitSelection
            ? "Указанный Python не запускается или старше версии 3.11: " + configured + ". Исправьте DOCUMENT_CONVERTER_PYTHON / DOCLING_PYTHON или удалите переменную для автоматического выбора."
            : "Не найден Python 3.11 или новее. Укажите полный путь к Python окружения конвертера в переменной DOCUMENT_CONVERTER_PYTHON (или DOCLING_PYTHON). Рекомендуется Python 3.12 с уже установленными зависимостями проекта.");
    }

    private static void Add(List<string> values, string value)
    {
        if (!String.IsNullOrWhiteSpace(value) && !values.Contains(value)) values.Add(value);
    }

    // Windows argument escaping, including paths ending in backslashes.
    private static string Quote(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\') { slashes++; continue; }
            if (character == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            slashes = 0;
            result.Append(character);
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    private static bool IsOurServer(int port)
    {
        try
        {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/api/health");
            request.Proxy = null;
            request.Timeout = 600;
            request.ReadWriteTimeout = 600;
            request.AllowAutoRedirect = false;
            request.KeepAlive = false;
            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
            {
                char[] buffer = new char[16384];
                int count = reader.Read(buffer, 0, buffer.Length);
                return response.StatusCode == HttpStatusCode.OK && new string(buffer, 0, count).IndexOf("\"" + AppMarker + "\"", StringComparison.Ordinal) >= 0;
            }
        }
        catch (WebException) { return false; }
        catch (IOException) { return false; }
    }

    private static bool IsPortAvailable(int port)
    {
        TcpListener listener = new TcpListener(IPAddress.Loopback, port);
        try { listener.Start(); return true; }
        catch (SocketException) { return false; }
        finally { listener.Stop(); }
    }

    private static int FindFreePort()
    {
        TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        try { return ((IPEndPoint)listener.LocalEndpoint).Port; }
        finally { listener.Stop(); }
    }

    private static void OpenBrowser(int port)
    {
        Process.Start(new ProcessStartInfo("http://127.0.0.1:" + port + "/") { UseShellExecute = true });
        Log("OPEN existing server | port=" + port);
    }

    private static string ReadLogTail(string path)
    {
        try
        {
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (StreamReader reader = new StreamReader(stream, Encoding.UTF8))
            {
                if (stream.Length > 4000) stream.Seek(-4000, SeekOrigin.End);
                return reader.ReadToEnd();
            }
        }
        catch (Exception) { return ""; }
    }

    private static string StableHash(string value)
    {
        using (SHA256 algorithm = SHA256.Create())
            return BitConverter.ToString(algorithm.ComputeHash(Encoding.UTF8.GetBytes(value.ToLowerInvariant()))).Replace("-", "").Substring(0, 24);
    }

    private static void Log(string message)
    {
        try { if (startupLog != null) File.AppendAllText(startupLog, DateTimeOffset.Now.ToString("o") + " " + message + Environment.NewLine, new UTF8Encoding(false)); }
        catch (Exception) { }
    }
}
