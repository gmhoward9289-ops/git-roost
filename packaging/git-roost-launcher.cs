// The winget package's Windows launcher, compiled to git-roost.exe at build
// time (see packaging/build-windows-zip.ps1). Replaces the earlier
// packaging/git-roost.cmd: winget's "portable" nested installer type
// requires an actual PE executable inside the zip, not a batch file --
// wingetcreate rejected the .cmd outright ("No supported installer(s) found
// in zip archive"), however faithfully it reproduced the interpreter probe.
//
// This is that same probe, in C#: find a Python, run git_roost.py with it,
// get out of the way. Same candidate order as bin/git-roost.js's Windows
// branch and the abandoned .cmd -- `py -3` first, because it exists even
// when `python` resolves to the Microsoft Store app-execution alias stub.
//
// Compiled via the C# compiler bundled with every Windows install's .NET
// Framework (through PowerShell's Add-Type), not the .NET SDK -- so building
// it needs nothing beyond what a stock Windows box already has, and running
// it needs nothing beyond Python itself. No new dependency reaches the user.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

class Launcher
{
    static readonly string[][] Candidates =
    {
        new[] { "py", "-3" },
        new[] { "python" },
        new[] { "python3" },
    };

    static int Main(string[] args)
    {
        string here = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string script = Path.Combine(here, "git_roost.py");
        string argTail = QuoteArgs(args);

        foreach (var candidate in Candidates)
        {
            string exe = candidate[0];
            string preArgs = candidate.Length > 1 ? candidate[1] + " " : "";
            var psi = new ProcessStartInfo
            {
                FileName = exe,
                Arguments = preArgs + Quote(script) + argTail,
                UseShellExecute = false,
            };
            try
            {
                using (var proc = Process.Start(psi))
                {
                    proc.WaitForExit();
                    return proc.ExitCode;
                }
            }
            catch (Win32Exception)
            {
                // exe not on PATH; try the next candidate.
                continue;
            }
        }

        Console.Error.WriteLine(
            "git-roost needs Python 3.9 or newer on PATH (tried: py -3, python, python3)");
        Console.Error.WriteLine("  https://www.python.org/downloads/");
        return 127;
    }

    // ProcessStartInfo.Arguments takes one literal command-line string, so
    // each argument is individually quoted and escaped -- good enough for
    // git-roost's actual flags (--root, --filter, paths), not a general
    // shell-quoting implementation.
    static string QuoteArgs(string[] args)
    {
        var sb = new StringBuilder();
        foreach (var a in args)
        {
            sb.Append(' ');
            sb.Append(Quote(a));
        }
        return sb.ToString();
    }

    static string Quote(string s)
    {
        return "\"" + s.Replace("\"", "\\\"") + "\"";
    }
}
