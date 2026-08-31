// Ported from milux-vault-sync android/brain Retriever.kt at 42a89de (ADR-001).
package uk.co.milux.vantagedeployed

import java.io.File
import kotlin.math.ln

/**
 * On-device BM25 retrieval over the phone's local vault, a faithful Kotlin mirror of
 * `src/vaultsync/vaultqa.py`. Pure logic, no Android dependency and no network, so the phone
 * retrieves what the box retrieves from the same vault, and so it is testable on the JVM against
 * the same fixtures as the Python.
 *
 * This is slice 1 of the on-device AI epic (see `docs/on-device-ai-design.md`): the retrieval and
 * clearance substrate that a later on-device model is grounded on. The clearance ceiling in
 * [Index.search] is enforced through [Classification], failing closed exactly as the box does.
 */
object Retriever {
    val SKIP_DIRS = setOf(".obsidian", ".git", ".stversions", ".stfolder", ".trash")
    private val TOKEN = Regex("[a-z0-9]+")

    fun tokenize(text: String): List<String> =
        TOKEN.findAll(text.lowercase()).map { it.value }.toList()

    data class Chunk(
        val note: String,        // vault-relative path, for citation
        val heading: String,     // nearest heading, for context
        val text: String,
        val tokens: List<String>,
        val level: String = "OFFICIAL",   // the note's classification tier, for clearance filtering
    )

    private fun noteLevel(content: String): String = Classification.readMarkingSafe(content).level

    fun chunkNote(rel: String, content: String, target: Int = 600): List<Chunk> {
        val level = noteLevel(content)
        var heading = ""
        val chunks = mutableListOf<Chunk>()
        var buf = mutableListOf<String>()
        var size = 0

        fun flush() {
            if (buf.isNotEmpty()) {
                val t = buf.joinToString("\n").trim()
                if (t.isNotEmpty()) chunks.add(Chunk(rel, heading, t, tokenize("$heading $t"), level))
                buf = mutableListOf()
                size = 0
            }
        }

        for (line in content.split("\n")) {
            val s = line.trim()
            if (s.startsWith("#")) {
                flush()
                heading = s.trimStart('#', ' ').trim()
                continue
            }
            if (s.isEmpty()) {
                if (size >= target) flush()
                continue
            }
            buf.add(line)
            size += line.length + 1
            if (size >= target) flush()
        }
        flush()
        return chunks
    }

    fun loadVault(vault: File): List<Chunk> {
        val chunks = mutableListOf<Chunk>()
        vault.walkTopDown()
            .onEnter { it.name !in SKIP_DIRS }
            .filter { it.isFile && it.name.endsWith(".md") }
            .forEach { f ->
                val content = runCatching { f.readText() }.getOrNull() ?: return@forEach
                chunks += chunkNote(f.relativeTo(vault).path, content)
            }
        return chunks
    }

    /** BM25 index over vault chunks. Mirror of `vaultqa.Index`. */
    class Index(val chunks: List<Chunk>, private val k1: Double = 1.5, private val b: Double = 0.75) {
        private val n = chunks.size
        private val df = HashMap<String, Int>()
        private val tf = ArrayList<HashMap<String, Int>>(n)
        private val avgdl: Double

        init {
            var total = 0
            for (c in chunks) {
                val counts = HashMap<String, Int>()
                for (tok in c.tokens) counts[tok] = (counts[tok] ?: 0) + 1
                tf.add(counts)
                total += c.tokens.size
                for (term in counts.keys) df[term] = (df[term] ?: 0) + 1
            }
            avgdl = if (n > 0) total.toDouble() / n else 0.0
        }

        private fun idf(term: String): Double {
            val d = df[term] ?: 0
            return ln(1 + (n - d + 0.5) / (d + 0.5))
        }

        /**
         * Retrieve top-k passages. If clearance is given, passages from notes above that tier are
         * excluded before ranking, so a model grounded on the results can never see or cite above
         * the asker's clearance. Fails closed: a clearance that cannot be placed on the ladder, or a
         * note whose level cannot be placed, withholds rather than leaks.
         */
        fun search(query: String, k: Int = 5, clearance: String? = null): List<Pair<Chunk, Double>> {
            var ceiling: Int? = null
            if (clearance != null) {
                // A clearance was requested but cannot be resolved: fail closed, return nothing.
                ceiling = try {
                    Classification.LADDER.indexOf(Classification.normalise(clearance))
                } catch (e: Classification.ClassificationError) {
                    return emptyList()
                }
            }
            val q = tokenize(query)
            val scored = ArrayList<Pair<Chunk, Double>>()
            for (i in chunks.indices) {
                val c = chunks[i]
                if (ceiling != null) {
                    val rank = Classification.LADDER.indexOf(c.level)
                    if (rank < 0) continue            // unknown level: withhold, do not leak
                    if (rank > ceiling) continue
                }
                val counts = tf[i]
                val dl = if (c.tokens.isNotEmpty()) c.tokens.size else 1
                var score = 0.0
                for (term in q) {
                    val f = counts[term] ?: 0
                    if (f == 0) continue
                    score += idf(term) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / (if (avgdl != 0.0) avgdl else 1.0)))
                }
                if (score > 0) scored.add(c to score)
            }
            // Stable descending sort, matching Python's list.sort(reverse=True) on ties.
            return scored.sortedByDescending { it.second }.take(k)
        }
    }

    /** `AI Context.md` at the vault root, if present: who is asking and how to answer,
     *  prepended to every prompt so the on-device model shares the box's grounding. */
    fun operatorContext(vault: File): String {
        val f = File(vault, "AI Context.md")
        val text = runCatching { f.readText() }.getOrNull()?.trim() ?: return ""
        return if (text.isEmpty()) "" else "OPERATOR CONTEXT:\n" + text.take(2000) + "\n\n"
    }

    const val GROUNDING =
        "You are an assistant answering strictly from the notes below, which come from a " +
        "deployed knowledge vault. Use only this context. If the answer is not in it, say " +
        "\"That is not in the notes.\" Be concise. After the answer, cite the note names you " +
        "used.\n\n"

    fun buildPrompt(question: String, hits: List<Pair<Chunk, Double>>): String {
        val ctx = hits.joinToString("\n\n") { (c, _) ->
            val head = if (c.heading.isNotEmpty()) " (${c.heading})" else ""
            "[${c.note}$head]\n${c.text}"
        }
        return "${GROUNDING}CONTEXT:\n$ctx\n\nQUESTION: $question\n\nANSWER:"
    }

    fun sources(hits: List<Pair<Chunk, Double>>): List<String> {
        val seen = mutableListOf<String>()
        for ((c, _) in hits) if (c.note !in seen) seen.add(c.note)
        return seen
    }
}
