package uk.co.milux.vantagedeployed

import android.content.Context
import android.net.Uri
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The phone half of the sync transport (Spec 004): fetch the signed index, pull what is new
 * or changed, push local edits with the hash they were based on. The Kotlin mirror of the
 * vaultapply decision rules the box enforces:
 *
 *   remote == local                      -> nothing to do.
 *   local untouched since last receive   -> safe to take the remote version.
 *   local edited, remote unchanged       -> push it (base = last received).
 *   both changed                         -> keep the local single version in the vault and
 *                                           stage the remote copy OUTSIDE the vault; a push
 *                                           the box refuses as 409 is the same state seen
 *                                           from the other side.
 *
 * The vault lives in app-private storage (files/vault); conflicts stage in files/conflicts,
 * never inside the vault, so a reader or a model can never pick up a second copy of a note.
 * Sync state is one map of vault-relative path to last-received hash.
 */
object SyncEngine {
    data class Result(val pulled: Int, val pushed: Int, val conflicts: Int,
                      val unchanged: Int, val packs: Int, val removed: Int, val error: String?) {
        fun line(): String = error?.let { "sync failed: $it" }
            ?: "pulled $pulled, pushed $pushed, unchanged $unchanged" +
                (if (removed > 0) ", removed $removed" else "") +
                (if (packs > 0) ", packs $packs" else "") +
                if (conflicts > 0) ", CONFLICTS $conflicts (staged for resolution)" else ""
    }

    fun vaultRoot(c: Context): File = File(c.filesDir, "vault").apply { mkdirs() }
    fun packsRoot(c: Context): File = File(c.filesDir, "packs").apply { mkdirs() }
    fun packsIndex(c: Context): List<Map<String, String>> {
        val f = File(c.filesDir, "packs-index.json")
        if (!f.exists()) return emptyList()
        val arr = org.json.JSONArray(f.readText())
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            o.keys().asSequence().associateWith { o.get(it).toString() }
        }
    }
    fun conflictsRoot(c: Context): File = File(c.filesDir, "conflicts").apply { mkdirs() }
    private fun stateFile(c: Context) = File(c.filesDir, "sync-state.json")

    fun sha256(b: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }

    // One last-received truth for BOTH bearers (IP and mesh), fine-grained locked so the
    // mesh service's writes and a running sync never clobber each other's entries.
    private val stateLock = Any()

    fun lastReceived(c: Context, rel: String): String? =
        synchronized(stateLock) { loadState(c)[rel] }

    fun recordReceived(c: Context, rel: String, hash: String) {
        synchronized(stateLock) {
            val m = loadState(c)
            m[rel] = hash
            saveState(c, m)
        }
    }

    fun removeReceived(c: Context, rel: String) {
        synchronized(stateLock) {
            val m = loadState(c)
            if (m.remove(rel) != null) saveState(c, m)
        }
    }

    // Device->box deletion tombstones: notes the operator deleted on the phone, remembered
    // (rel -> the base hash the deletion was based on) so the next sync tells the box, then
    // forgets. A tombstone distinguishes "the operator deleted this" from "the phone has not
    // pulled this yet"; without it the pull phase would just re-create the note.
    private val delLock = Any()
    private fun delFile(c: Context) = File(c.filesDir, "deletions.json")

    private fun loadDeletions(c: Context): MutableMap<String, String> {
        val f = delFile(c)
        if (!f.exists()) return mutableMapOf()
        val o = JSONObject(f.readText())
        return o.keys().asSequence().associateWithTo(mutableMapOf()) { o.getString(it) }
    }

    private fun saveDeletions(c: Context, m: Map<String, String>) {
        val o = JSONObject(); m.forEach { (k, v) -> o.put(k, v) }
        val tmp = File(delFile(c).path + ".tmp"); tmp.writeText(o.toString()); tmp.renameTo(delFile(c))
    }

    /** The operator deletes a note on the phone: remove the local file, drop its received
     *  state, and remember it as a pending deletion so the next sync removes it on the box. */
    fun deleteNote(c: Context, rel: String) {
        synchronized(delLock) {
            val base = lastReceived(c, rel) ?: "force"
            val m = loadDeletions(c); m[rel] = base; saveDeletions(c, m)
        }
        File(vaultRoot(c), rel).delete()
        removeReceived(c, rel)
    }

    private fun loadState(c: Context): MutableMap<String, String> {
        val f = stateFile(c)
        if (!f.exists()) return mutableMapOf()
        val o = JSONObject(f.readText())
        return o.keys().asSequence().associateWithTo(mutableMapOf()) { o.getString(it) }
    }

    private fun saveState(c: Context, state: Map<String, String>) {
        val o = JSONObject()
        state.forEach { (k, v) -> o.put(k, v) }
        val tmp = File(stateFile(c).path + ".tmp")
        tmp.writeText(o.toString())
        tmp.renameTo(stateFile(c))
    }

    /** Every markdown file under the local vault, vault-relative. */
    fun localNotes(c: Context): List<String> {
        val root = vaultRoot(c)
        return root.walkTopDown().filter { it.isFile && it.name.endsWith(".md") }
            .map { it.relativeTo(root).path }.sorted().toList()
    }

    fun stagedConflicts(c: Context): Int =
        conflictsRoot(c).walkTopDown().count { it.isFile }

    /** One staged conflict: the box's version sits outside the vault, the local version is in
     *  it. `rel` is the vault note both refer to, recovered by stripping the conflict infix. */
    data class Conflict(val staged: File, val rel: String, val stagedHash: String)

    private val CONFLICT_INFIX = Regex("""\.sync-conflict-\d{8}-\d{6}-.*(\.md)$""")

    fun conflicts(c: Context): List<Conflict> {
        val root = conflictsRoot(c)
        return root.walkTopDown().filter { it.isFile }.map { f ->
            val stagedRel = f.relativeTo(root).path
            val rel = stagedRel.replaceFirst(CONFLICT_INFIX, "$1")
            Conflict(f, rel, sha256(f.readBytes()))
        }.sortedBy { it.rel }.toList()
    }

    /** Take the box's version: write the staged content into the vault note and record it as
     *  received, so the next sync sees local == box and nothing moves. Resolved. */
    fun resolveTakeTheirs(c: Context, k: Conflict) {
        val note = File(vaultRoot(c), k.rel)
        note.parentFile?.mkdirs()
        val tmp = File(note.path + ".tmp")
        tmp.writeBytes(k.staged.readBytes())
        tmp.renameTo(note)
        recordReceived(c, k.rel, k.stagedHash)
        k.staged.delete()
    }

    /** Keep the local version: discard the staged copy and advance the note's base to the
     *  box's current version (the staged hash), so the next sync fast-forwards the box to the
     *  local edit instead of conflicting again. The local vault note is left untouched. */
    fun resolveKeepMine(c: Context, k: Conflict) {
        recordReceived(c, k.rel, k.stagedHash)
        k.staged.delete()
    }

    fun sync(c: Context, binding: Binding): Result {
        var pulled = 0; var pushed = 0; var conflicts = 0; var unchanged = 0; var packs = 0
        var removed = 0
        val root = vaultRoot(c)
        val state = synchronized(stateLock) { loadState(c) }  // snapshot for the deletion pass
        val tombstones = synchronized(delLock) { loadDeletions(c) }
        try {
            val idx = Api.signed(c, binding, "GET", "/sync/index")
            if (idx.code != 200) return Result(0, 0, 0, 0, 0, 0, "index refused (${idx.code})")
            val files = idx.body.getJSONArray("files")
            val remote = mutableMapOf<String, String>()
            for (i in 0 until files.length()) {
                val e = files.getJSONObject(i)
                remote[e.getString("path")] = e.getString("sha256")
            }

            // Pull phase: the vaultapply decision, phone side. A note the operator has just
            // deleted (a live tombstone) is NOT re-pulled; the delete phase settles it.
            for ((rel, remoteHash) in remote) {
                if (tombstones.containsKey(rel)) continue
                val local = File(root, rel)
                val localHash = if (local.exists()) sha256(local.readBytes()) else null
                val lastRecv = lastReceived(c, rel)
                when {
                    localHash == remoteHash -> { recordReceived(c, rel, remoteHash); unchanged++ }
                    remoteHash == lastRecv -> { /* local edit only: the push phase's job */ }
                    localHash == null || localHash == lastRecv -> {
                        val got = Api.signed(c, binding, "GET",
                            "/sync/file?path=${Uri.encode(rel)}")
                        if (got.code != 200) return Result(pulled, pushed, conflicts,
                            unchanged, packs, removed, "pull refused for $rel (${got.code})")
                        if (sha256(got.bytes) != remoteHash) return Result(pulled, pushed,
                            conflicts, unchanged, packs, removed, "hash mismatch pulling $rel")
                        local.parentFile?.mkdirs()
                        val tmp = File(local.path + ".tmp")
                        tmp.writeBytes(got.bytes)
                        tmp.renameTo(local)
                        recordReceived(c, rel, remoteHash)
                        pulled++
                    }
                    else -> {
                        // Both sides changed: keep the local single version, stage remote
                        // OUTSIDE the vault (never beside the note).
                        val got = Api.signed(c, binding, "GET",
                            "/sync/file?path=${Uri.encode(rel)}")
                        if (got.code == 200) {
                            val ts = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.UK)
                                .format(Date())
                            val q = File(conflictsRoot(c),
                                rel.replace(".md", ".sync-conflict-$ts-${binding.box}.md"))
                            q.parentFile?.mkdirs()
                            q.writeBytes(got.bytes)
                        }
                        conflicts++
                    }
                }
            }

            // Deletion pass (box -> device): a note in our received-state that the box no
            // longer lists is one the box deleted. Remove it locally only if we have not
            // edited it since we received it, so a local edit is never silently destroyed;
            // an edited-but-box-deleted note is left to push back (re-create) rather than lost.
            for (rel in state.keys.toList()) {
                if (remote.containsKey(rel)) continue
                val local = File(root, rel)
                if (!local.exists()) { removeReceived(c, rel); continue }
                if (sha256(local.readBytes()) == state[rel]) {
                    local.delete()
                    removeReceived(c, rel)
                    removed++
                }
            }

            // Device->box deletion phase: send each tombstone, then forget the ones the box
            // accepted (or that were already gone). A conflict means the box changed the note
            // since the phone last saw it; the delete loses, the tombstone is cleared, and the
            // note re-pulls on a later sync (the box's version wins, no data lost).
            if (tombstones.isNotEmpty()) {
                val remaining = HashMap(tombstones)
                for ((rel, base) in tombstones) {
                    val r = Api.signed(c, binding, "POST",
                        "/sync/delete?path=${Uri.encode(rel)}&base=$base")
                    when (r.code) {
                        200 -> { remaining.remove(rel); removed++ }
                        409 -> remaining.remove(rel)
                        else -> { }
                    }
                }
                synchronized(delLock) { saveDeletions(c, remaining) }
            }

            // Push phase: anything local whose hash is not what we last received.
            for (rel in localNotes(c)) {
                val bytes = File(root, rel).readBytes()
                val localHash = sha256(bytes)
                val lastRecv = lastReceived(c, rel)
                if (lastRecv == localHash) continue
                val base = lastRecv ?: "new"
                val r = Api.signed(c, binding, "POST",
                    "/sync/push?path=${Uri.encode(rel)}&base=$base", bytes)
                when (r.code) {
                    200 -> { recordReceived(c, rel, localHash); pushed++ }
                    409 -> conflicts++      // the box staged our version; its copy stands there
                    else -> return Result(pulled, pushed, conflicts, unchanged, packs, removed,
                        "push refused for $rel (${r.code})")
                }
            }
            // Pack phase (Spec 005): the manifest's sha256 is the truth; pull what is
            // missing or changed, verify every byte, delete nothing.
            val pk = Api.signed(c, binding, "GET", "/sync/packs")
            if (pk.code == 200) {
                val arr = pk.body.getJSONArray("packs")
                val indexFile = File(c.filesDir, "packs-index.json")
                indexFile.writeText(arr.toString())
                for (i in 0 until arr.length()) {
                    val e = arr.getJSONObject(i)
                    val name = e.getString("name")
                    val want = e.getString("sha256")
                    val f = File(packsRoot(c), name)
                    if (f.exists() && sha256(f.readBytes()) == want) continue
                    val got = Api.signed(c, binding, "GET",
                        "/sync/pack?name=${Uri.encode(name)}")
                    if (got.code != 200 || sha256(got.bytes) != want) {
                        return Result(pulled, pushed, conflicts, unchanged, packs, removed,
                            "pack $name failed verification")
                    }
                    val tmp = File(f.path + ".tmp")
                    tmp.writeBytes(got.bytes)
                    tmp.renameTo(f)
                    packs++
                }
            }
            return Result(pulled, pushed, conflicts, unchanged, packs, removed, null)
        } catch (e: Exception) {
            return Result(pulled, pushed, conflicts, unchanged, packs, removed, e.javaClass.simpleName)
        }
    }
}
