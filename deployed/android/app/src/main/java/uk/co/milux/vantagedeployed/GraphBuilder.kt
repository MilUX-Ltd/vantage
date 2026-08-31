// Ported from milux-vault-sync android/brain GraphBuilder.kt at 42a89de (ADR-001); package renamed,
// vault rebased on the product's synced app-private store.
package uk.co.milux.vantagedeployed

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Builds the knowledge graph from the LOCAL vault on the phone, offline. A faithful port of
 * vaultsync/graph.py and the layered ontology in docs/research/intelligence-ontology.md: an
 * entity (person, organisation, place, facility, materiel, event) colours by its affiliation
 * and kind (hostile red, neutral civil ochre, friendly olive, terrain green); a product
 * (orders, intelligence, reporting, ...) colours by its kind. Edges come from wikilinks, typed
 * relational frontmatter, and operation membership.
 */
object GraphBuilder {
    private val SKIP = setOf(".obsidian", ".git", ".stversions", ".stfolder", ".trash")
    private val WIKILINK = Regex("""\[\[([^\]|#]+)""")
    private val FM = Regex("""^---\s*\n(.*?)\n---\s*\n""", setOf(RegexOption.DOT_MATCHES_ALL))

    private val REF_KEYS = setOf("refs", "references", "refers_to", "amends", "supersedes",
        "parent", "part_of", "task_org", "objective", "about",
        "location", "covers", "from", "net", "nets", "owning", "unit",
        "member_of", "commands", "commanded_by", "located_at", "owns", "owned_by",
        "operates", "aligned_with", "opposes", "reports_to", "adjacent_to",
        "answers", "answered_by", "indicators", "assigned_to", "order")

    private val ENTITY_TYPES = setOf("person", "organisation", "place", "facility", "materiel", "event")

    private val PRODUCT_FAMILY = linkedMapOf(
        "orders" to listOf("opord", "frago", "order", "intent", "conops", "task"),
        "intelligence" to listOf("intsum", "intrep", "ipb", "osint", "threat-assessment",
            "pattern-of-life", "human-terrain-analysis", "atmospherics", "ground-picture"),
        "reporting" to listOf("sitrep", "contact-report", "sigevent", "report",
            "reporting-triage", "tak-chat-log", "log"),
        "comms" to listOf("pace"),
        "command" to listOf("deployment-index", "operation", "decision-log", "battle-rhythm", "orbat",
            "ccir", "pir", "ffir", "eei"),
    )

    private val FAMILY_COLOUR = mapOf(
        "orders" to "#C0503B", "intelligence" to "#7B5EA7", "hostile" to "#B23A48",
        "human-terrain" to "#C08A2E", "friendly" to "#B5B171", "reporting" to "#586F7C",
        "places" to "#4E7A51", "materiel" to "#6E7F94", "comms" to "#3E8E8E",
        "command" to "#113308", "other" to "#9AA0A6",
    )
    private val LEGEND_ORDER = listOf("orders", "intelligence", "hostile", "human-terrain",
        "friendly", "reporting", "places", "materiel", "comms", "command", "other")
    private const val OTHER = "#9AA0A6"

    private val LEGACY_FAMILY = mapOf(
        "threat" to "hostile", "threat-unit" to "hostile", "callsign" to "friendly",
        "key-leader" to "human-terrain", "population" to "human-terrain", "settlement" to "places",
        "named-area" to "places", "location" to "places", "human-terrain" to "intelligence")

    private val OBJECT_TYPES = ENTITY_TYPES + PRODUCT_FAMILY.values.flatten().toSet() + LEGACY_FAMILY.keys

    private fun listVals(v: String?): List<String> {
        var s = (v ?: "").trim()
        if (s.startsWith("[") && s.endsWith("]")) s = s.substring(1, s.length - 1)
        return s.split(",").map { it.trim().trim('"', '\'').lowercase() }.filter { it.isNotEmpty() }
    }

    private fun resolveFamily(fm: Map<String, String>): String {
        val typ = (fm["type"] ?: "").trim().lowercase()
        var ent = (fm["entity"] ?: "").trim().lowercase()
        if (ent.isEmpty() && typ in ENTITY_TYPES) ent = typ
        val aff = (fm["affiliation"] ?: "").trim().lowercase()
        if (ent.isNotEmpty()) {
            if (ent == "place" || ent == "facility") return "places"
            if (aff == "hostile") return "hostile"
            if (aff == "neutral" || aff == "unknown")
                return if (ent == "person" || ent == "organisation") "human-terrain"
                       else if (ent == "materiel") "materiel" else "other"
            if (ent == "person" || ent == "organisation") return "friendly"
            if (ent == "materiel") return "materiel"
            return "other"
        }
        for ((family, types) in PRODUCT_FAMILY) if (typ in types) return family
        return LEGACY_FAMILY[typ] ?: "other"
    }

    private fun frontmatter(text: String): Map<String, String> {
        val m = FM.find(text) ?: return emptyMap()
        val fm = HashMap<String, String>()
        for (line in m.groupValues[1].lines()) {
            val i = line.indexOf(':')
            if (i > 0) fm[line.substring(0, i).trim().lowercase()] = line.substring(i + 1).trim()
        }
        return fm
    }

    private fun opLabel(raw: String?): String {
        var v = (raw ?: "").trim().trim('"', '\'')
        WIKILINK.find(v)?.let { v = it.groupValues[1] }
        return v.trim()
    }

    fun build(vaultDir: File): String {
        data class Node(val id: String, val label: String, val type: String, val entity: String,
                        val affiliation: String, val roles: List<String>, val family: String,
                        val colour: String, val classification: String, val operation: String,
                        val unlabelled: Boolean, val path: String)
        val nodes = LinkedHashMap<String, Node>()
        val rawLinks = ArrayList<Pair<String, String>>()
        val opOf = LinkedHashMap<String, String>()
        val indexFor = HashMap<String, String>()

        vaultDir.walkTopDown()
            .onEnter { it.name !in SKIP }
            .filter { it.isFile && it.name.endsWith(".md") }
            .forEach { f ->
                val text = runCatching { f.readText() }.getOrNull() ?: return@forEach
                val id = f.nameWithoutExtension
                val fm = frontmatter(text)
                val type = (fm["type"] ?: "").trim().lowercase()
                val entity = (fm["entity"] ?: "").trim().lowercase()
                val op = opLabel(fm["operation"])
                if (type !in OBJECT_TYPES && entity !in ENTITY_TYPES && op.isEmpty()) return@forEach

                val family = resolveFamily(fm)
                val label = (fm["title"]?.trim('"', ' ')?.ifEmpty { null }) ?: id
                val clazz = (fm["classification"] ?: "OFFICIAL").split(" ").firstOrNull()?.ifEmpty { "OFFICIAL" } ?: "OFFICIAL"
                nodes[id] = Node(id, label, type.ifEmpty { "note" },
                    entity.ifEmpty { if (type in ENTITY_TYPES) type else "" },
                    (fm["affiliation"] ?: "").trim().lowercase(), listVals(fm["roles"]),
                    family, FAMILY_COLOUR[family] ?: OTHER, clazz, op, op.isEmpty(),
                    f.relativeTo(vaultDir).path)
                if (op.isNotEmpty()) {
                    opOf[id] = op
                    if (type == "deployment-index") indexFor.getOrPut(op) { id }
                }
                WIKILINK.findAll(text).forEach { rawLinks.add(id to it.groupValues[1].trim()) }
                for (k in REF_KEYS) fm[k]?.let { v ->
                    WIKILINK.findAll(v).forEach { rawLinks.add(id to it.groupValues[1].trim()) }
                }
            }

        for (op in opOf.values.toSortedSet()) {
            if (indexFor[op] == null) {
                val hub = "operation:$op"
                if (hub !in nodes) nodes[hub] = Node(hub, op, "operation", "", "", emptyList(),
                    "command", FAMILY_COLOUR["command"] ?: OTHER, "OFFICIAL", op, false, "")
                indexFor[op] = hub
            }
        }
        for ((id, op) in opOf) {
            val hub = indexFor[op]!!
            if (hub != id) rawLinks.add(id to hub)
        }

        val links = JSONArray()
        val seen = HashSet<Pair<String, String>>()
        val deg = HashMap<String, Int>()
        for ((src, target) in rawLinks) {
            val tid = if (target in nodes) target else File(target).nameWithoutExtension
            if (tid in nodes && tid != src && (src to tid) !in seen) {
                seen.add(src to tid)
                links.put(JSONObject().put("source", src).put("target", tid))
                deg[src] = (deg[src] ?: 0) + 1
                deg[tid] = (deg[tid] ?: 0) + 1
            }
        }
        val nodeArr = JSONArray()
        for (n in nodes.values) nodeArr.put(JSONObject()
            .put("id", n.id).put("label", n.label).put("type", n.type).put("entity", n.entity)
            .put("affiliation", n.affiliation).put("roles", JSONArray(n.roles))
            .put("family", n.family).put("colour", n.colour)
            .put("classification", n.classification).put("operation", n.operation)
            .put("unlabelled", n.unlabelled).put("path", n.path).put("degree", deg[n.id] ?: 0))
        val present = nodes.values.map { it.family }.toSet()
        val legend = JSONArray()
        for (fam in LEGEND_ORDER) if (fam in present)
            legend.put(JSONObject().put("family", fam).put("colour", FAMILY_COLOUR[fam]))
        return JSONObject().put("nodes", nodeArr).put("links", links).put("legend", legend).toString()
    }
}
