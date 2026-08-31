package uk.co.milux.shared.mesh

/**
 * Phone-to-box sender for one fileset transfer: the mirror of bin/vault-mesh-send-files.py.
 *
 * The caller supplies a `send` that puts one packet on the radio (pacing/threading is the
 * caller's, so this stays a pure protocol object) and an `onDone` for the outcome. `start()`
 * sends the manifest and all fragments once; the caller then feeds every STATUS packet the box
 * returns to `onStatus`, which retransmits exactly what the box still lacks. The box confirms with
 * an explicit `complete` flag (see repair.is_delivered): an empty missing-set is not treated as
 * done, because an integrity failure would show the same. Give up after a run of no-progress
 * rounds so a dead link does not retransmit forever.
 *
 * The caller (BridgeService in the app) routes inbound STATUS packets here and decides when to
 * start a transfer from local edits; it shares the one radio with receive. See the app's BACKLOG.
 */
class MeshSender(
    private val transferId: Long,
    payload: ByteArray,
    private val send: (ByteArray) -> Unit,
    private val onDone: (delivered: Boolean) -> Unit,
    private val noProgressLimit: Int = 20,
    private val manifestReps: Int = 3,
) {
    private val packets = Encoder.encodeTransfer(payload, transferId)
    private val manifest = packets.first()
    private val data = packets.drop(1)
    private var finished = false
    private var noProgress = 0
    private var bestMissing = data.size + 1
    private var bestHaveManifest = false
    private var prevMissing: List<Int>? = null

    /** Send the whole payload once. Repair then proceeds as the box's status packets arrive. */
    fun start() {
        for (p in packets) send(p)
    }

    /** Feed one STATUS packet from the box. Retransmits what is missing, or finishes. */
    fun onStatus(st: StatusPkt) {
        if (finished || st.transferId != transferId) return
        if (st.complete) { finish(true); return }
        if (!st.haveManifest) repeat(manifestReps) { send(manifest) }
        val missing = st.missingRanges.flatMap { (it[0]..it[1]).toList() }.filter { it in data.indices }
        for (i in missing) send(data[i])
        // progress: manifest arrived, fewer missing, or the receiver is asking for a different set
        // (a status names at most MAX_RANGES holes, so a large lossy transfer advances without the
        // count ever dropping below the best-seen; treat a changed request set as progress too).
        val missingNow = if (st.haveManifest) missing.size else data.size
        val progressed = (st.haveManifest && !bestHaveManifest) ||
            missingNow < bestMissing ||
            (missing.isNotEmpty() && missing != prevMissing)
        prevMissing = missing
        bestHaveManifest = bestHaveManifest || st.haveManifest
        bestMissing = minOf(bestMissing, missingNow)
        if (progressed) noProgress = 0
        else if (++noProgress >= noProgressLimit) finish(false)
    }

    private fun finish(delivered: Boolean) {
        if (finished) return
        finished = true
        onDone(delivered)
    }
}
