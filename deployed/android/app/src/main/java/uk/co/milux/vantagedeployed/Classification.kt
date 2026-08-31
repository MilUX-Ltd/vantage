// Ported from milux-vault-sync android/brain Classification.kt at 42a89de (ADR-001).
package uk.co.milux.vantagedeployed

/**
 * Security classification as an enforced control, not a label. A faithful Kotlin mirror of
 * `src/vaultsync/classification.py`, so the phone judges a note's marking exactly as the box does.
 * On-device retrieval (see [Retriever]) uses this to withhold anything above the asker's clearance,
 * failing closed: an unreadable marking is treated as TOP SECRET and withheld rather than leaked.
 *
 * This is the load-bearing part of the on-device AI epic. It is a control, so the port is
 * line-for-line rather than approximate, and it is tested against the same vectors as the Python.
 */
object Classification {
    // Ordered low to high. OFFICIAL-SENSITIVE sits above OFFICIAL for movement decisions.
    val LADDER = listOf("OFFICIAL", "OFFICIAL-SENSITIVE", "SECRET", "TOP SECRET")

    val NATO_EQUIV = mapOf(
        "NATO UNCLASSIFIED" to "OFFICIAL",
        "NATO RESTRICTED" to "OFFICIAL-SENSITIVE",
        "NATO CONFIDENTIAL" to "SECRET",
        "NATO SECRET" to "SECRET",
        "COSMIC TOP SECRET" to "TOP SECRET",
    )

    /** A marking that cannot be understood. Fail closed: treat as the highest tier. */
    class ClassificationError(message: String) : Exception(message)

    data class Marking(
        val level: String,
        val releasableTo: Set<String> = emptySet(),
        val caveats: Set<String> = emptySet(),
        val raw: String = "",
    ) {
        fun rank(): Int = LADDER.indexOf(level)
    }

    fun normalise(level: String): String {
        val s = level.trim().uppercase()
        if (s in LADDER) return s
        NATO_EQUIV[s]?.let { return it }
        if (s.replace(" ", "-") == "OFFICIAL-SENSITIVE") return "OFFICIAL-SENSITIVE"
        throw ClassificationError("unknown classification: $level")
    }

    /**
     * Pull the classification tier from the start of a marking string, ignoring trailing REL TO /
     * caveats. Longest known marking wins (OFFICIAL-SENSITIVE before OFFICIAL). Fails closed.
     */
    fun leadingLevel(value: String): String {
        val s = value.trim().uppercase()
        val candidates = (LADDER + NATO_EQUIV.keys).sortedByDescending { it.length }
        for (c in candidates) {
            if (s == c || s.startsWith("$c ") || s.startsWith("$c,") || s.startsWith("$c/")) return normalise(c)
        }
        if (s.startsWith("OFFICIAL SENSITIVE")) return "OFFICIAL-SENSITIVE"
        throw ClassificationError("no leading classification in $value")
    }

    private val FM = Regex("^---\\s*\\n(.*?)\\n---\\s*\\n", RegexOption.DOT_MATCHES_ALL)
    private val REL = Regex("REL\\s+TO\\s+([A-Z/ ,]+)", RegexOption.IGNORE_CASE)
    private val SPLIT = Regex("[/, ]+")
    private val SEMI = Regex("[;,]")

    /**
     * A note's marking from its frontmatter. No classification field means OFFICIAL, the GSC
     * default for routine business and the vault's baseline. Throws on an unreadable marking.
     */
    fun readMarking(noteText: String): Marking {
        val block = FM.find(noteText)?.groupValues?.get(1) ?: ""
        var level = "OFFICIAL"
        val caveats = mutableSetOf<String>()
        val rel = mutableSetOf<String>()
        for (line in block.split("\n")) {
            val colon = line.indexOf(':')
            if (colon < 0) continue
            val key = line.substring(0, colon).trim().lowercase()
            val vRaw = line.substring(colon + 1)
            val v = vRaw.trim().trim('[', ']').trim()
            when {
                key == "classification" && v.isNotEmpty() -> {
                    level = leadingLevel(v.split("//")[0])
                    REL.find(vRaw)?.let { m ->
                        rel += m.groupValues[1].split(SPLIT).map { it.trim().uppercase() }.filter { it.isNotEmpty() }
                    }
                }
                key in setOf("caveat", "caveats", "handling") && v.isNotEmpty() ->
                    caveats += v.split(SEMI).map { it.trim().uppercase() }.filter { it.isNotEmpty() }
                key in setOf("releasable_to", "rel_to", "releasability") && v.isNotEmpty() ->
                    rel += v.split(SPLIT).map { it.trim().uppercase() }.filter { it.isNotEmpty() }
            }
        }
        return Marking(level, rel, caveats, block)
    }

    /** As [readMarking], but never throws: an unreadable marking becomes TOP SECRET (fail closed). */
    fun readMarkingSafe(noteText: String): Marking =
        try {
            readMarking(noteText)
        } catch (e: ClassificationError) {
            Marking("TOP SECRET", raw = "unreadable-marking")
        }
}
