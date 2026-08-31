// Ported from milux-vault-sync android/brain NewEntityActivity.kt at 42a89de (ADR-001); package renamed,
// vault rebased on the product's synced app-private store.
package uk.co.milux.vantagedeployed

import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import java.io.File

/**
 * Create a first-class graph entity by filling in its attributes, not by hand-writing frontmatter.
 * Pick a type in the Vault Viewer, fill the fields that matter for that type (a person has an
 * affiliation and roles; a place has a grid), write the note, and Save composes the correct
 * ontology frontmatter and body and writes it into the vault. The entity then colours and places
 * itself on the knowledge graph and, if it has a grid, can be published to TAK.
 *
 * The frontmatter shape follows the layered ontology (docs/research/intelligence-ontology.md):
 * `type`/`entity` for what it is, `affiliation` as a separate layer, `operation` for the
 * deployment, `roles` for actors, `lat`/`lon` for anything with a position. Everything is OFFICIAL
 * and EXERCISE, the deployed brain's baseline.
 */
class NewEntityActivity : AppCompatActivity() {
    private lateinit var vault: File
    private lateinit var folder: String
    private lateinit var type: String

    private lateinit var name: EditText
    private lateinit var operation: EditText
    private var affiliation: Spinner? = null
    private var roles: EditText? = null
    private var lat: EditText? = null
    private var lon: EditText? = null
    private lateinit var body: EditText

    private val green = Color.parseColor("#113308")
    private val greyBlue = Color.parseColor("#586F7C")
    private val neutral = Color.parseColor("#F7F6EB")

    private val AFFILIATIONS = listOf("Friendly", "Hostile", "Neutral", "Unknown")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        vault = File(intent.getStringExtra("vault") ?: Prefs.vault(this))
        folder = intent.getStringExtra("folder") ?: ""
        type = intent.getStringExtra("type") ?: "person"
        title = "New ${type.replaceFirstChar { it.uppercase() }}"

        val showAffiliation = type in setOf("person", "organisation", "materiel", "event")
        val showRoles = type in setOf("person", "organisation")
        val showGeo = type in setOf("place", "facility", "materiel", "event")

        val d = resources.displayMetrics.density
        val pad = (16 * d).toInt()
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }

        val where = folder.ifEmpty { "the vault root" }
        root.addView(TextView(this).apply {
            text = "A new ${type} in $where. Fill what you know; you can edit the rest after."
            setTextColor(greyBlue); setPadding(0, 0, 0, pad)
        })

        name = field(root, "Name", "e.g. Elder Rahim")
        if (showAffiliation) {
            root.addView(label("Affiliation"))
            affiliation = Spinner(this).apply {
                adapter = ArrayAdapter(this@NewEntityActivity, android.R.layout.simple_spinner_dropdown_item, AFFILIATIONS)
            }
            root.addView(affiliation)
        }
        if (showRoles) roles = field(root, "Roles (comma separated)", "e.g. commander, elder")
        operation = field(root, "Deployment", "e.g. Operation Sentinel")
        operation.setText(Deployment.labelFor(vault, folder.substringBefore('/')))   // the deployment's label
        if (showGeo) {
            val geoRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            lat = EditText(this).apply { hint = "Latitude, e.g. 51.2345"; inputType = android.text.InputType.TYPE_CLASS_NUMBER or
                android.text.InputType.TYPE_NUMBER_FLAG_DECIMAL or android.text.InputType.TYPE_NUMBER_FLAG_SIGNED }
            lon = EditText(this).apply { hint = "Longitude, e.g. -1.9876"; inputType = android.text.InputType.TYPE_CLASS_NUMBER or
                android.text.InputType.TYPE_NUMBER_FLAG_DECIMAL or android.text.InputType.TYPE_NUMBER_FLAG_SIGNED }
            root.addView(label("Position (decimal degrees)"))
            geoRow.addView(lat, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            geoRow.addView(lon, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            root.addView(geoRow)
            root.addView(TextView(this).apply {
                text = "Not a grid reference. North and east are positive."
                textSize = 12f; setTextColor(greyBlue)
            })
        }
        root.addView(label("Notes"))
        body = EditText(this).apply { hint = "What is known about this ${type}"; minLines = 4; gravity = Gravity.TOP }
        root.addView(body)

        root.addView(Button(this).apply {
            text = "Create ${type}"; setTextColor(neutral); setBackgroundColor(green)
            setPadding(pad, pad, pad, pad)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply { topMargin = pad }
            setOnClickListener { create() }
        })

        setContentView(ScrollView(this).apply { addView(root) })
    }

    private fun label(text: String): TextView {
        val d = resources.displayMetrics.density
        return TextView(this).apply {
            this.text = text; setTextColor(green); setTypeface(typeface, Typeface.BOLD)
            setPadding(0, (12 * d).toInt(), 0, (2 * d).toInt())
        }
    }

    private fun field(root: LinearLayout, labelText: String, hintText: String): EditText {
        root.addView(label(labelText))
        val e = EditText(this).apply { hint = hintText; setSingleLine() }
        root.addView(e)
        return e
    }

    private fun create() {
        val nm = name.text.toString().trim()
        if (nm.isEmpty()) {
            name.error = "Give it a name"
            return
        }
        val la = lat?.text?.toString()?.trim()?.toDoubleOrNull()
        val lo = lon?.text?.toString()?.trim()?.toDoubleOrNull()
        if ((la != null && (la < -90 || la > 90)) || (lo != null && (lo < -180 || lo > 180))) {
            (if (la != null && (la < -90 || la > 90)) lat else lon)?.error =
                "Latitude runs -90 to 90 and longitude -180 to 180. Decimal degrees, not a grid reference."
            return
        }
        val fileName = nm.replace(Regex("[/\\\\]"), "-") + ".md"
        val dir = File(vault, folder)
        val f = File(dir, fileName)
        if (f.exists()) {
            Toast.makeText(this, "A note named $nm already exists here", Toast.LENGTH_LONG).show()
            openNote(f.relativeTo(vault).path)
            return
        }
        val ok = runCatching { f.writeText(compose(nm)) }.isSuccess
        if (!ok) {
            Toast.makeText(this, "Could not create the note", Toast.LENGTH_LONG).show()
            return
        }
        Toast.makeText(this, "Created on this phone. It will reach the box when you have WiFi or mesh.", Toast.LENGTH_SHORT).show()
        openNote(f.relativeTo(vault).path)
        finish()
    }

    /** Compose the ontology frontmatter from the form, then the body. */
    private fun compose(nm: String): String {
        val sb = StringBuilder("---\n")
        sb.append("type: $type\n")
        sb.append("entity: $type\n")
        sb.append("title: \"$nm\"\n")
        val op = operation.text.toString().trim()
        if (op.isNotEmpty()) sb.append("operation: $op\n")
        affiliation?.let { sb.append("affiliation: ${AFFILIATIONS[it.selectedItemPosition].lowercase()}\n") }
        roles?.text?.toString()?.trim()?.let { r ->
            val list = r.split(",").map { it.trim() }.filter { it.isNotEmpty() }
            if (list.isNotEmpty()) sb.append("roles: [${list.joinToString(", ")}]\n")
        }
        val la = lat?.text?.toString()?.trim() ?: ""
        val lo = lon?.text?.toString()?.trim() ?: ""
        if (la.isNotEmpty() && lo.isNotEmpty()) { sb.append("lat: $la\n"); sb.append("lon: $lo\n") }
        sb.append("classification: OFFICIAL\n")
        sb.append("handling: EXERCISE\n")
        sb.append("---\n\n# $nm\n\n")
        sb.append(body.text.toString().trim())
        sb.append("\n")
        return sb.toString()
    }

    override fun onBackPressed() {
        val dirty = name.text.isNotBlank() || body.text.isNotBlank()
        if (!dirty) { super.onBackPressed(); return }
        androidx.appcompat.app.AlertDialog.Builder(this)
            .setTitle("Discard this ${type}?")
            .setMessage("Nothing has been created yet.")
            .setPositiveButton("Discard") { _, _ -> finish() }
            .setNegativeButton("Keep editing", null).show()
    }

    private fun openNote(rel: String) {
        startActivity(android.content.Intent(this, NoteActivity::class.java)
            .putExtra("vault", vault.path).putExtra("path", rel))
    }
}
