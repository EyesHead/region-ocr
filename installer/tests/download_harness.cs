using System;
using System.Collections.Generic;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;

internal static class DownloadHarness
{
    [STAThread]
    static int Main(string[] args)
    {
        ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
        Console.OutputEncoding = new UTF8Encoding(false);
        using (var wizard = new Wizard())
        using (var stop = new CancellationTokenSource())
        using (var timer = new Timer(delegate { stop.Cancel(); }, null,
            args.Length > 4 ? Int32.Parse(args[4]) : Timeout.Infinite, Timeout.Infinite))
        {
            var item = new Dictionary<string, object> {
                { "url", args[0] }, { "filename", "python.zip" },
                { "bytes", Int64.Parse(args[2]) }, { "sha256", args[3] }
            };
            try
            {
                typeof(Wizard).GetMethod("Download", BindingFlags.Instance | BindingFlags.NonPublic)
                    .Invoke(wizard, new object[] { item, args[1], stop.Token });
                Console.WriteLine("DOWNLOAD OK");
                return 0;
            }
            catch (TargetInvocationException error)
            {
                var cause = error.InnerException;
                Console.WriteLine(cause.GetType().Name + ": " + cause.Message);
                return cause is OperationCanceledException ? 2 : 1;
            }
        }
    }
}
