// Ported from milux-vault-sync android/app BridgeService.kt at 42a89de (ADR-001): the
// field-proven mesh transfer machinery, rewired to the product vault and the one shared
// last-received state, so the IP and mesh bearers can never fight over a file.
package uk.co.milux.vantagedeployed

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.ServiceConnection
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import org.meshtastic.core.model.DataPacket
import org.meshtastic.core.service.IMeshService
import uk.co.milux.shared.mesh.MeshSender
import uk.co.milux.shared.mesh.Parser
import uk.co.milux.shared.mesh.Reassembler
import uk.co.milux.shared.mesh.StatusPkt
import uk.co.milux.shared.mesh.Wire
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.zip.ZipInputStream

/**
 * Foreground service holding the Meshtastic binding and transfer state. Foreground plus a
 * runtime receiver registered here (not a manifest receiver) is what reliably catches the
 * app's rebroadcast in the background. START_STICKY plus a rebind retry keep it alive
 * across process pressure and Meshtastic reconnects.
 */
class MeshBearerService : Service() {

    companion object {
        const val MESH_PACKAGE = "com.geeksville.mesh"
        const val BIND_ACTION = "com.geeksville.mesh.Service"
        const val EXTRA_PAYLOAD = "com.geeksville.mesh.Payload"
        const val CHANNEL_ID = "vantage_mesh_quiet"

        @Volatile var status: String = "starting"
        @Volatile var lastFile: String = "nothing yet"
        @Volatile var received: Int = 0
        private var instance: MeshBearerService? = null
        fun deliver(dp: DataPacket) { instance?.onPacket(dp) }
        fun isRunning() = instance != null
    }

    private var service: IMeshService? = null
    private var reassembler: Reassembler? = null
    private var currentId: Long = -1
    private var port = MeshPrefs.DEFAULT_PORT
    // Transfers already delivered, so a re-sent fragment (the sender not yet having heard our
    // COMPLETE) is re-acknowledged rather than rebuilt and rewritten. Bounded LRU.
    private val completedIds = object : LinkedHashMap<Long, Boolean>(16, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<Long, Boolean>?): Boolean = size > 64
    }
    // Conflicts stage OUTSIDE the vault, in the same place the IP bearer stages them.
    private val conflictDir by lazy { SyncEngine.conflictsRoot(this) }
    private lateinit var vaultDir: File
    private val main = Handler(Looper.getMainLooper())

    // Reverse sync (phone -> box). The feature has a master enable (reverseEnabled), but when on it
    // is automatic: it only actually sends when the phone is OFF WiFi, so it never competes with
    // Syncthing on an IP link. reverseSync is the effective state = reverseEnabled && !wifiOn.
    // All outbound state below is touched only on the main thread.
    private var reverseEnabled = false
    private var wifiOn = false
    private var reverseSync = false
    private var scanning = false
    private var cm: android.net.ConnectivityManager? = null
    private var wifiCb: android.net.ConnectivityManager.NetworkCallback? = null
    private var activeSender: MeshSender? = null
    private var activeTid: Long = -1
    private var activeBatch: List<Pair<String, String>> = emptyList()  // rel -> hash, recorded as received on delivery
    private val sendQueue = ArrayDeque<ByteArray>()
    private var draining = false
    private var outboundTimeout: Runnable? = null

    private fun log(s: String) { status = s; Log.i("MeshBearer", s) }

    private val runtimeReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, i: Intent) {
            val dp = try {
                if (Build.VERSION.SDK_INT >= 33) i.getParcelableExtra(EXTRA_PAYLOAD, DataPacket::class.java)
                else @Suppress("DEPRECATION") i.getParcelableExtra(EXTRA_PAYLOAD)
            } catch (t: Throwable) { Log.w("MeshBearer", "unmarshal failed: $t"); null }
            if (dp != null) onPacket(dp)
        }
    }

    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            service = IMeshService.Stub.asInterface(binder)
            try {
                service?.subscribeReceiver(packageName, "uk.co.milux.vantagedeployed.MeshPacketReceiver")
                log("connected to Meshtastic (${service?.myId}); listening on port $port")
            } catch (t: Throwable) { log("connected; subscribe failed: $t") }
        }
        override fun onServiceDisconnected(name: ComponentName) {
            service = null; log("Meshtastic disconnected; will retry")
            main.postDelayed({ bindMesh() }, 5000)
        }
    }

    private fun bindMesh() {
        if (service != null) return
        val intent = Intent(BIND_ACTION).apply { setPackage(MESH_PACKAGE) }
        if (!bindService(intent, conn, Context.BIND_AUTO_CREATE)) {
            log("Meshtastic not bindable; is it installed and connected? retrying")
            main.postDelayed({ bindMesh() }, 8000)
        }
    }

    override fun onCreate() {
        super.onCreate()
        instance = this
        port = MeshPrefs.port(this)
        reverseEnabled = MeshPrefs.reverse(this)
        vaultDir = SyncEngine.vaultRoot(this)
        startForeground(1, notification("Mesh bearer active"))
        val filter = IntentFilter().apply { MeshPrefs.receiveActions(port).forEach { addAction(it) } }
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(runtimeReceiver, filter, Context.RECEIVER_EXPORTED)
        else @Suppress("UnspecifiedRegisterReceiverFlag") registerReceiver(runtimeReceiver, filter)
        bindMesh()
        registerWifiWatch()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        super.onDestroy()
        // cancel every posted callback (the scan loop, drain, timeout, acks, bind retries) so this
        // instance does not live on as a zombie sending on a stale binder after a restart
        main.removeCallbacksAndMessages(null)
        reverseSync = false; reverseEnabled = false; scanning = false
        activeSender = null; sendQueue.clear()
        wifiCb?.let { try { cm?.unregisterNetworkCallback(it) } catch (_: Throwable) {} }; wifiCb = null
        try { unregisterReceiver(runtimeReceiver) } catch (_: Throwable) {}
        try { unbindService(conn) } catch (_: Throwable) {}
        service = null
        instance = null
    }

    fun onPacket(dp: DataPacket) {
        val bytes = dp.bytes ?: return
        val body = Wire.open(bytes) ?: return
        if (body.size < 8) return
        // a STATUS packet is the box acking one of OUR outbound transfers; route it to the sender
        // (on the main thread, where all outbound state lives) and stop. Manifest/data fall through.
        if (Parser.parse(bytes) is StatusPkt) {
            val st = Parser.parse(bytes) as StatusPkt
            if (st.transferId == activeTid) main.post { activeSender?.onStatus(st) }
            return
        }
        val tid = ((body[4].toLong() and 0xFF) shl 24) or ((body[5].toLong() and 0xFF) shl 16) or
                ((body[6].toLong() and 0xFF) shl 8) or (body[7].toLong() and 0xFF)
        // Already delivered: the sender has not yet heard our COMPLETE and is re-sending. Re-ack
        // and stop; do not rebuild or rewrite the files.
        if (completedIds.containsKey(tid)) { ackComplete(tid, 2); return }
        var r = reassembler
        if (r == null || currentId != tid) { r = Reassembler(tid); reassembler = r; currentId = tid }
        val done = r.feed(bytes)
        r.manifest?.let { log("receiving ${it.totalFrags - r.missing().size}/${it.totalFrags} parts") }
        try {
            service?.send(DataPacket("^all", 0, r.status().encode(), port).apply { wantAck = false; hopLimit = 1 })
        } catch (_: Throwable) {}
        if (done != null) {
            val files = unpack(done)
            received += files.size
            lastFile = files.joinToString(", ")
            log("vault updated: $lastFile")
            notify("Received: ${files.joinToString(", ")}")
            completedIds[tid] = true
            ackComplete(tid, 4)   // send COMPLETE several times so the box reliably hears it
            reassembler = null; currentId = -1
        }
    }

    /** Send a COMPLETE status for a delivered transfer several times, spaced out, so a single
     *  lost packet does not leave the sender re-sending forever. Non-blocking (posted, not slept). */
    private fun ackComplete(tid: Long, times: Int) {
        val s = StatusPkt(tid, true, true, emptyList()).encode()
        for (i in 0 until times) {
            main.postDelayed({
                try { service?.send(DataPacket("^all", 0, s, port).apply { wantAck = false; hopLimit = 1 }) }
                catch (_: Throwable) {}
            }, i * 220L)
        }
    }

    private fun unpack(payload: ByteArray): List<String> {
        val written = ArrayList<String>()
        ZipInputStream(ByteArrayInputStream(payload)).use { zin ->
            var e = zin.nextEntry
            while (e != null) {
                val rel = e.name
                val parts = rel.split("/")
                if (!rel.startsWith("/") && !rel.endsWith("/") &&
                    parts.none { it.isEmpty() || it == "." || it == ".." }) {
                    applyIncoming(rel, zin.readBytes(), written)
                }
                e = zin.nextEntry
            }
        }
        return written
    }

    /**
     * Apply one received file without ever silently overwriting a local edit. The decision:
     *  - no local file            -> write it (new).
     *  - local == incoming        -> nothing to do (identical).
     *  - local == last delivered  -> the user has not touched it since; safe to overwrite.
     *  - otherwise                -> the user edited it locally; keep their single version in the
     *                                vault and stage the incoming one OUTSIDE the vault (app private
     *                                storage) for resolution, so a reader never sees two copies.
     */
    private fun applyIncoming(rel: String, incoming: ByteArray, written: ArrayList<String>) {
        val out = File(vaultDir, rel)
        if (out.isDirectory) return                         // an entry colliding with a directory
        out.parentFile?.mkdirs()
        val incomingHash = sha256Hex(incoming)
        if (!out.exists()) {
            atomicWrite(out, incoming)
            SyncEngine.recordReceived(this, rel, incomingHash)
            written.add(rel); return
        }
        val localHash = sha256Hex(out.readBytes())
        if (localHash == incomingHash) {                    // already identical
            SyncEngine.recordReceived(this, rel, incomingHash); return
        }
        if (localHash == SyncEngine.lastReceived(this, rel)) {  // untouched since last delivery
            atomicWrite(out, incoming)
            SyncEngine.recordReceived(this, rel, incomingHash)
            written.add(rel); return
        }
        val cf = File(conflictDir, conflictName(rel))       // stage OUTSIDE the vault, keep local
        cf.parentFile?.mkdirs(); atomicWrite(cf, incoming)
        written.add(cf.path)
        log("conflict on $rel: kept local edit, staged out of vault at ${cf.path}")
    }

    /** Write via a temp file and rename, so a kill mid-write cannot leave a truncated note. */
    private fun atomicWrite(target: File, data: ByteArray) {
        val tmp = File(target.parentFile, target.name + ".tmp")
        tmp.outputStream().use { it.write(data) }
        if (!tmp.renameTo(target)) {                        // fallback if rename is refused
            target.outputStream().use { it.write(data) }
            tmp.delete()
        }
    }

    private fun conflictName(rel: String): String {
        val slash = rel.lastIndexOf('/')
        val dot = rel.lastIndexOf('.')
        val hasExt = dot > slash + 1                        // a leading-dot name has no extension
        val base = if (hasExt) rel.substring(0, dot) else rel
        val ext = if (hasExt) rel.substring(dot) else ""
        val ts = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        return "$base.sync-conflict-$ts-MESH$ext"
    }

    private fun sha256Hex(b: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }

    // ---- reverse sync: send local edits back to the box over the mesh (opt-in, off by default) ----

    /** Watch WiFi so reverse sync runs only when off-grid (WiFi off), never against Syncthing. */
    private fun registerWifiWatch() {
        cm = getSystemService(Context.CONNECTIVITY_SERVICE) as? android.net.ConnectivityManager
        wifiOn = hasWifi()
        val req = android.net.NetworkRequest.Builder()
            .addTransportType(android.net.NetworkCapabilities.TRANSPORT_WIFI).build()
        val cb = object : android.net.ConnectivityManager.NetworkCallback() {
            override fun onAvailable(n: android.net.Network) { main.post { wifiOn = hasWifi(); updateReverse() } }
            override fun onLost(n: android.net.Network) { main.post { wifiOn = hasWifi(); updateReverse() } }
        }
        wifiCb = cb
        try { cm?.registerNetworkCallback(req, cb) } catch (_: Throwable) {}
        updateReverse()
    }

    private fun hasWifi(): Boolean = try {
        cm?.allNetworks?.any {
            cm?.getNetworkCapabilities(it)?.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI) == true
        } ?: false
    } catch (_: Throwable) { false }

    /** Effective reverse sync = enabled AND off WiFi. Start the scan loop when it turns on. */
    private fun updateReverse() {
        val eff = reverseEnabled && !wifiOn
        if (eff == reverseSync) return
        reverseSync = eff
        if (eff) { log("off WiFi: reverse mesh sync active"); startScanning() }
        else log("reverse mesh sync idle (on WiFi or disabled)")
    }

    private fun startScanning() {
        if (scanning) return
        scanning = true
        scanTick()
    }

    private fun scanTick() {
        if (!reverseSync) { scanning = false; return }
        scanAndSend()
        main.postDelayed({ scanTick() }, 45_000)
    }

    /** If nothing is in flight, gather up to a small batch of locally-edited notes and send them. */
    private fun scanAndSend() {
        if (!reverseSync || activeSender != null || service == null) return
        val batch = filesToSend()
        if (batch.isEmpty()) return
        startOutbound(batch)
    }

    /** Notes changed on the EUD since we last received or sent them (markdown only), freshest
     *  first so a just-made edit reaches the box ahead of backfilling the rest. Bounded batch. */
    private fun filesToSend(): List<Triple<String, ByteArray, String>> {
        val cands = ArrayList<Pair<Long, Triple<String, ByteArray, String>>>()
        for (f in vaultDir.walkTopDown()) {
            if (!f.isFile || !f.name.endsWith(".md") || f.path.contains("/.")) continue
            if (f.length() > 64L * 1024) continue   // notes are small; an oversized file cannot fit
            val rel = f.relativeTo(vaultDir).path
            val bytes = try { f.readBytes() } catch (_: Throwable) { continue }
            val h = sha256Hex(bytes)
            if (h != SyncEngine.lastReceived(this, rel)) {
                cands.add(f.lastModified() to Triple(rel, bytes, h))
            }
        }
        return cands.sortedByDescending { it.first }.take(4).map { it.second }
    }

    private fun startOutbound(batch: List<Triple<String, ByteArray, String>>) {
        try {
            val payload = zipFileset(batch.map { it.first to it.second })
            val tid = System.nanoTime() and 0xffffffffL
            activeTid = tid
            activeBatch = batch.map { it.first to it.third }
            activeSender = MeshSender(tid, payload, send = { sendQueue.add(it) },
                onDone = { delivered -> main.post { finishOutbound(delivered) } })
            log("sending ${batch.size} local edit(s) back over the mesh")
            activeSender?.start()
            startDrain()
            outboundTimeout = Runnable { finishOutbound(false) }
            main.postDelayed(outboundTimeout!!, 180_000)   // give up if never confirmed
        } catch (t: Throwable) {                           // e.g. a fileset too big to encode
            log("could not start outbound send: $t")
            activeSender = null; activeTid = -1; activeBatch = emptyList(); sendQueue.clear()
        }
    }

    private fun finishOutbound(delivered: Boolean) {
        outboundTimeout?.let { main.removeCallbacks(it) }; outboundTimeout = null
        if (delivered) {
            for ((rel, h) in activeBatch) SyncEngine.recordReceived(this, rel, h)
            log("edits delivered to the box (${activeBatch.size})")
        } else if (activeSender != null) {
            log("edit send did not confirm; will retry")
        }
        activeSender = null; activeTid = -1; activeBatch = emptyList(); sendQueue.clear()
    }

    private fun startDrain() {
        if (draining) return
        draining = true
        drainOne()
    }

    /** Pace one packet at a time onto the radio, so a burst does not overrun the mesh queue. */
    private fun drainOne() {
        val pkt = sendQueue.removeFirstOrNull()
        if (pkt != null) {
            try {
                service?.send(DataPacket("^all", 0, pkt, port).apply { wantAck = false; hopLimit = 1 })
            } catch (_: Throwable) {}
        }
        if (activeSender != null || sendQueue.isNotEmpty()) main.postDelayed({ drainOne() }, 1200)
        else draining = false
    }

    private fun zipFileset(files: List<Pair<String, ByteArray>>): ByteArray {
        val bos = ByteArrayOutputStream()
        ZipOutputStream(bos).use { z ->
            for ((rel, bytes) in files) { z.putNextEntry(ZipEntry(rel)); z.write(bytes); z.closeEntry() }
        }
        return bos.toByteArray()
    }

    private fun notification(text: String): Notification {
        if (Build.VERSION.SDK_INT >= 26) {
            val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
            // The status notification is a service anchor, not news: no launcher badge, minimum
            // prominence. New channel id because Android keeps a channel's first settings forever.
            nm.deleteNotificationChannel("vantage_mesh")
            val ch = NotificationChannel(CHANNEL_ID, "Mesh bearer", NotificationManager.IMPORTANCE_MIN)
            ch.setShowBadge(false)
            nm.createNotificationChannel(ch)
        }
        val open = android.app.PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            android.app.PendingIntent.FLAG_IMMUTABLE
        )
        val b = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL_ID)
                else @Suppress("DEPRECATION") Notification.Builder(this)
        return b.setContentTitle("Mesh bearer").setContentText(text)
            .setSmallIcon(R.drawable.ic_launcher_foreground).setContentIntent(open)
            .setOngoing(true).build()
    }

    private fun notify(text: String) {
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).notify(1, notification(text))
    }
}
