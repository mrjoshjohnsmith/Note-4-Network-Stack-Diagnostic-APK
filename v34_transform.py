from pathlib import Path

SERVICE = Path('hbc-src/app/src/main/java/com/highbrightnesscontrol/note4/HbmService.java')
BRIDGE = Path('hbc-src/app/src/main/java/com/highbrightnesscontrol/note4/PowerBrightnessBridge.java')
GRADLE = Path('hbc-src/app/build.gradle')

s = SERVICE.read_text()
old_call = '            applyScaledAutoBrightness(effective);'
new_call = '            applyScaledAutoBrightness(sensitivity);'
assert s.count(old_call) == 1
s = s.replace(old_call, new_call)

start = s.index('    private void applyScaledAutoBrightness(')
end = s.index('    private void stopPowerBridge()', start)
new_apply = '''    private void applyScaledAutoBrightness(int sensitivity) {
        if (powerBridgeFailed) return;
        if (sensitivity < 0) sensitivity = 0;
        if (sensitivity > 1000000) sensitivity = 1000000;

        // Reserved tiny-negative adjustment encoding consumed by the CTI1
        // AutomaticBrightnessController framework hook:
        // factor = sensitivity / 100
        // encoded = -(factor + 1) / 2^30
        float factor = sensitivity / 100f;
        float encoded = -((factor + 1f) / 1073741824f);
        String command = Float.toString(encoded);

        if (powerBridgeBase.length() == 0) {
            powerBridgeBase = new File(getFilesDir(), "hbc_power_" + Process.myPid()).getAbsolutePath();
            powerBridgeCommandPath = powerBridgeBase + ".cmd";
            powerBridgeStatusPath = powerBridgeBase + ".status";
            powerBridgeTokenPath = powerBridgeBase + ".run";
        }

        if (!powerBridgeLaunchRequested) {
            deleteLocal(powerBridgeStatusPath);
            if (!writeLocal(powerBridgeCommandPath, command)
                    || !writeLocal(powerBridgeTokenPath, "1")) {
                powerBridgeFailed = true;
                return;
            }

            String apk = getApplicationInfo().sourceDir;
            String launch = "CLASSPATH=" + RootShell.sq(apk) + " app_process /system/bin "
                    + PowerBrightnessBridge.class.getName() + " "
                    + RootShell.sq(powerBridgeCommandPath) + " " + RootShell.sq(powerBridgeStatusPath) + " "
                    + RootShell.sq(powerBridgeTokenPath) + " " + Process.myPid();
            RootShell.Result r = RootShell.run("sh -c " + RootShell.sq("(" + launch + ") >/dev/null 2>&1 </dev/null &"));
            if (!r.ok()) {
                powerBridgeFailed = true;
                deleteLocal(powerBridgeTokenPath);
                return;
            }
            powerBridgeLaunchRequested = true;
            powerBridgeLaunchTime = System.currentTimeMillis();
            lastScaledBrightnessTarget = sensitivity;
        } else if (sensitivity != lastScaledBrightnessTarget) {
            if (writeLocal(powerBridgeCommandPath, command)) {
                lastScaledBrightnessTarget = sensitivity;
            }
        }

        String status = readLocal(powerBridgeStatusPath);
        if (status.startsWith("OK:")) {
            powerBridgeReady = true;
            autoBrightnessOverrideActive = true;
        } else if (status.startsWith("READY")) {
            powerBridgeReady = true;
        } else if (status.startsWith("ERR")) {
            powerBridgeFailed = true;
            releaseScaledAutoBrightness();
        } else if (powerBridgeLaunchRequested
                && System.currentTimeMillis() - powerBridgeLaunchTime > 2000L) {
            powerBridgeFailed = true;
            releaseScaledAutoBrightness();
        }
    }

'''
s = s[:start] + new_apply + s[end:]

start = s.index('    private void releaseScaledAutoBrightness()')
end = s.index('    private boolean writeLocal(', start)
new_release = '''    private void releaseScaledAutoBrightness() {
        if (powerBridgeLaunchRequested && powerBridgeCommandPath.length() > 0) {
            writeLocal(powerBridgeCommandPath, "NaN");
        }
        stopPowerBridge();
        autoBrightnessOverrideActive = false;
        lastScaledBrightnessTarget = Integer.MIN_VALUE;
    }

'''
s = s[:start] + new_release + s[end:]
SERVICE.write_text(s)

BRIDGE.write_text(r'''package com.highbrightnesscontrol.note4;

import android.os.IBinder;
import android.os.Parcel;
import android.util.Log;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileReader;
import java.io.FileWriter;
import java.io.InputStreamReader;
import java.lang.reflect.Field;
import java.lang.reflect.Method;

public final class PowerBrightnessBridge {
    private static final String TAG = "HBCPowerBridge";
    private static final String DESCRIPTOR = "android.os.IPowerManager";

    private static void writeStatus(String path, String value) {
        FileWriter w = null;
        try {
            w = new FileWriter(path, false);
            w.write(value);
            w.write('\n');
            w.flush();
        } catch (Throwable ignored) {
        } finally {
            try { if (w != null) w.close(); } catch (Throwable ignored) {}
        }
    }

    private static String readFirstLine(String path) {
        BufferedReader r = null;
        try {
            r = new BufferedReader(new FileReader(path));
            String line = r.readLine();
            return line == null ? "" : line.trim();
        } catch (Throwable ignored) {
            return "";
        } finally {
            try { if (r != null) r.close(); } catch (Throwable ignored) {}
        }
    }

    private static boolean ownerAlive(int pid) {
        File f = new File("/proc/" + pid + "/cmdline");
        if (!f.isFile()) return false;
        FileInputStream in = null;
        try {
            in = new FileInputStream(f);
            byte[] b = new byte[512];
            int n = in.read(b);
            if (n <= 0) return false;
            String cmd = new String(b, 0, n, "UTF-8").replace('\0', ' ');
            return cmd.indexOf("com.highbrightnesscontrol.note4") >= 0;
        } catch (Throwable ignored) {
            return false;
        } finally {
            try { if (in != null) in.close(); } catch (Throwable ignored) {}
        }
    }

    private static int adjustmentTransactionCode() throws Exception {
        Class<?> stub = Class.forName("android.os.IPowerManager$Stub");
        Field field = stub.getDeclaredField(
                "TRANSACTION_setTemporaryScreenAutoBrightnessAdjustmentSettingOverride");
        field.setAccessible(true);
        return field.getInt(null);
    }

    private static void setAutoAdjustment(IBinder binder, int transactionCode, float target)
            throws Exception {
        Parcel data = Parcel.obtain();
        Parcel reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            data.writeFloat(target);
            if (!binder.transact(transactionCode, data, reply, 0)) {
                throw new IllegalStateException("power transaction not handled");
            }
            reply.readException();
        } finally {
            reply.recycle();
            data.recycle();
        }
    }

    public static void main(String[] args) {
        if (args == null || args.length != 4) return;
        String commandPath = args[0];
        String statusPath = args[1];
        String tokenPath = args[2];
        int ownerPid;
        try { ownerPid = Integer.parseInt(args[3]); }
        catch (Throwable t) { writeStatus(statusPath, "ERR:ARGS"); return; }

        IBinder binder = null;
        int transactionCode = -1;
        float last = Float.NaN;
        boolean applied = false;
        try {
            Class<?> serviceManager = Class.forName("android.os.ServiceManager");
            Method getService = serviceManager.getDeclaredMethod("getService", String.class);
            getService.setAccessible(true);
            binder = (IBinder)getService.invoke(null, "power");
            if (binder == null) throw new IllegalStateException("power service unavailable");

            transactionCode = adjustmentTransactionCode();
            writeStatus(statusPath, "READY:AUTO_ADJ:" + transactionCode);

            while (new File(tokenPath).isFile() && ownerAlive(ownerPid)) {
                String line = readFirstLine(commandPath);
                if (line.length() > 0) {
                    try {
                        float target = Float.parseFloat(line);
                        if (Float.floatToIntBits(target) != Float.floatToIntBits(last)) {
                            setAutoAdjustment(binder, transactionCode, target);
                            last = target;
                            applied = true;
                            writeStatus(statusPath, "OK:" + line);
                        }
                    } catch (Throwable t) {
                        Log.e(TAG, "auto adjustment command failed", t);
                        writeStatus(statusPath, "ERR:CMD:" + t.getClass().getSimpleName());
                        break;
                    }
                }
                try { Thread.sleep(50L); } catch (InterruptedException ignored) {}
            }
        } catch (Throwable t) {
            Log.e(TAG, "auto adjustment bridge failed", t);
            writeStatus(statusPath, "ERR:BRIDGE:" + t.getClass().getSimpleName());
        } finally {
            if (binder != null && transactionCode >= 0 && applied) {
                try { setAutoAdjustment(binder, transactionCode, Float.NaN); }
                catch (Throwable t) { Log.e(TAG, "auto adjustment release failed", t); }
            }
            writeStatus(statusPath, "RELEASED");
        }
    }
}
''')

g = GRADLE.read_text()
assert g.count('versionCode 9') == 1
assert g.count("versionName '3.3 Final'") == 1
g = g.replace('versionCode 9', 'versionCode 10')
g = g.replace("versionName '3.3 Final'", "versionName '3.4 Lux Test'")
GRADLE.write_text(g)
