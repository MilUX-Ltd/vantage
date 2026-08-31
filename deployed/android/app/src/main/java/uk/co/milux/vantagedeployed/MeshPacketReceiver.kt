package uk.co.milux.vantagedeployed

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import org.meshtastic.core.model.DataPacket

/**
 * Manifest-declared receiver. The Meshtastic app delivers received data to a subscribed
 * external app by an EXPLICIT broadcast (setClassName to this class by name), which only
 * a real manifest component receives, and which arrives even while this app is in the
 * background. It hands the packet to the foreground BridgeService.
 */
class MeshPacketReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val dp = if (Build.VERSION.SDK_INT >= 33)
            intent.getParcelableExtra(MeshBearerService.EXTRA_PAYLOAD, DataPacket::class.java)
        else @Suppress("DEPRECATION") intent.getParcelableExtra(MeshBearerService.EXTRA_PAYLOAD)
        if (dp == null) { Log.i("MeshBearer", "broadcast ${intent.action} with no payload"); return }
        Log.i("MeshBearer", "RX ${intent.action}: ${dp.bytes?.size ?: 0} B port ${dp.dataType}")
        MeshBearerService.deliver(dp)
    }
}
