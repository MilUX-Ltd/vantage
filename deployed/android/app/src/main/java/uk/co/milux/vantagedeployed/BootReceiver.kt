package uk.co.milux.vantagedeployed

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

/** Starts the mesh bearer on boot (and on app update) so it runs unattended. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (!MeshPrefs.enabled(context)) return
        val svc = Intent(context, MeshBearerService::class.java)
        try {
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(svc)
            else context.startService(svc)
        } catch (_: Throwable) { /* FGS-from-boot can be restricted; app launch will start it */ }
    }
}
