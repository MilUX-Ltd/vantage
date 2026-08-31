// Ported from milux-vault-sync android/brain SearchActivity.kt at 42a89de (ADR-001); package renamed,
// vault rebased on the product's synced app-private store.
package uk.co.milux.vantagedeployed

import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import java.io.File

/**
 * Vault Viewer: the active deployment's notes as one expandable tree, the way Obsidian's file
 * pane works. Folders open and close in place (no forward-and-back navigation), notes open to
 * read or edit, and typing two letters in the search box switches the list to search results.
 * New (+) creates an entity or note in the folder you choose. Everything is offline over the
 * phone's synced vault copy; a saved edit or a new note syncs home like any local change.
 */
class SearchActivity : AppCompatActivity() {
    private val h = Handler(Looper.getMainLooper())
    private lateinit var vault: File          // the vault root (paths stay vault-relative)
    private lateinit var scopeDir: File       // the active deployment folder, or the vault root
    private val expanded = mutableSetOf<String>()
    private var lastFolder: File? = null      // the folder the operator last opened; New targets it
    private lateinit var box: EditText
    private lateinit var results: LinearLayout
    private lateinit var status: TextView
    private var seq = 0

    private val green = Color.parseColor("#113308")
    private val greyBlue = Color.parseColor("#586F7C")
    private val beige = Color.parseColor("#D2C78D")
    private val gold = Color.parseColor("#7A7433")
    private val skip = setOf(".obsidian", ".git", ".stversions", ".stfolder", ".trash")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        vault = File(Prefs.vault(this))
        val dep = Prefs.deployment(this)
        scopeDir = if (dep.isEmpty()) vault else File(vault, dep)
        title = if (dep.isEmpty()) "Vault Viewer" else Deployment.labelFor(vault, dep)
        val d = resources.displayMetrics.density
        val pad = (16 * d).toInt()
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }

        val topRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        box = EditText(this).apply { hint = "Search notes"; textSize = 16f; setSingleLine() }
        topRow.addView(box, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        topRow.addView(Button(this).apply {
            text = "New"; setTextColor(Color.parseColor("#F7F6EB")); setBackgroundColor(green)
            setOnClickListener { newChooser() }
        })
        root.addView(topRow)
        status = TextView(this).apply { setTextColor(greyBlue); setPadding(0, pad / 2, 0, pad / 2) }
        root.addView(status)
        results = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(results)
        setContentView(ScrollView(this).apply { addView(root) })

        if (!scopeDir.isDirectory) {
            status.text = "Nothing at ${scopeDir.path}.\nLet Vault Bridge or Syncthing sync, or change deployment."
            box.isEnabled = false
            return
        }
        box.addTextChangedListener(object : TextWatcher {
            override fun afterTextChanged(s: Editable?) {
                val q = s?.toString()?.trim() ?: ""
                h.removeCallbacksAndMessages("run")
                if (q.length < 2) { renderTree(); return }
                status.text = "Searching..."
                h.postAtTime({ run(q) }, "run", android.os.SystemClock.uptimeMillis() + 250)
            }
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
        })
        renderTree()
        if (intent.getBooleanExtra("capture", false)) newChooser()
    }

    override fun onResume() {
        super.onResume()
        if (::box.isInitialized && box.text.toString().trim().length < 2 && scopeDir.isDirectory) renderTree()
    }

    // --- the tree ---

    private fun renderTree() {
        results.removeAllViews()
        status.text = "Tap a folder to open it. Tap a note to read or edit it."
        addLevel(scopeDir, 0)
        if (results.childCount == 0) {
            results.addView(TextView(this).apply { text = "No notes here yet. Tap New to create one."; setTextColor(greyBlue) })
        }
    }

    private fun addLevel(dir: File, depth: Int) {
        val entries = dir.listFiles()?.toList() ?: return
        val dirs = entries.filter { it.isDirectory && !it.name.startsWith(".") && it.name !in skip }
            .sortedBy { it.name.lowercase() }
        val notes = entries.filter { it.isFile && it.name.endsWith(".md") }.sortedBy { it.name.lowercase() }
        for (sub in dirs) {
            val rel = sub.relativeTo(vault).path
            val open = rel in expanded
            results.addView(row((if (open) "▾  " else "▸  ") + sub.name, depth, isFolder = true) {
                if (open) expanded.remove(rel) else { expanded.add(rel); lastFolder = sub }
                renderTree()
            })
            if (open) addLevel(sub, depth + 1)
        }
        for (note in notes) {
            results.addView(row(note.name.removeSuffix(".md"), depth, isFolder = false) {
                openNote(note.relativeTo(vault).path, false)
            })
        }
    }

    private fun row(label: String, depth: Int, isFolder: Boolean, onClick: () -> Unit): View {
        val d = resources.displayMetrics.density
        return TextView(this).apply {
            text = label
            textSize = if (isFolder) 16f else 15f
            setTextColor(if (isFolder) green else Color.parseColor("#2E4A26"))
            if (isFolder) setTypeface(typeface, Typeface.BOLD)
            setPadding((depth * 22 * d).toInt(), (11 * d).toInt(), 0, (11 * d).toInt())
            isClickable = true
            setOnClickListener { onClick() }
        }
    }

    // --- new ---

    /** New: pick what to create. Entity types open the attribute form; a plain note is the fallback. */
    private fun targetDir(): File = lastFolder?.takeIf { it.isDirectory } ?: scopeDir

    fun newChooser() {
        val dir = targetDir()
        val labels = arrayOf("Person", "Organisation", "Place", "Facility", "Materiel", "Event", "Plain note")
        val where = dir.relativeTo(vault).path.ifEmpty { "the vault root" }
        AlertDialog.Builder(this).setTitle("New in $where")
            .setItems(labels) { _, i ->
                if (labels[i] == "Plain note") plainNoteDialog()
                else startActivity(android.content.Intent(this, NewEntityActivity::class.java)
                    .putExtra("vault", vault.path)
                    .putExtra("folder", dir.relativeTo(vault).path)
                    .putExtra("type", labels[i].lowercase()))
            }
            .setNegativeButton("Cancel", null).show()
    }

    private fun plainNoteDialog() {
        val dir = targetDir()
        val d = resources.displayMetrics.density
        val input = EditText(this).apply { hint = "Note name"; setSingleLine() }
        val where = dir.relativeTo(vault).path.ifEmpty { "the vault root" }
        AlertDialog.Builder(this).setTitle("New note in $where")
            .setView(LinearLayout(this).apply {
                setPadding((20 * d).toInt(), (8 * d).toInt(), (20 * d).toInt(), 0); addView(input)
            })
            .setPositiveButton("Create note") { _, _ ->
                val name = input.text.toString().trim().replace(Regex("[/\\\\]"), "-")
                if (name.isEmpty()) return@setPositiveButton
                val f = File(dir, "$name.md")
                if (!f.exists()) {
                    val dep = Prefs.deployment(this)
                    val depLabel = if (dep.isEmpty()) "" else Deployment.labelFor(vault, dep)
                    val fm = StringBuilder("---\n")
                    fm.append("title: \"$name\"\n")
                    if (depLabel.isNotEmpty()) fm.append("operation: $depLabel\n")
                    fm.append("classification: OFFICIAL\nhandling: EXERCISE\n---\n\n# $name\n\n")
                    runCatching { f.writeText(fm.toString()) }.onFailure {
                        Toast.makeText(this, "Could not create the note", Toast.LENGTH_LONG).show()
                        return@setPositiveButton
                    }
                }
                openNote(f.relativeTo(vault).path, edit = true)
            }
            .setNegativeButton("Cancel", null).show()
    }

    private fun openNote(rel: String, edit: Boolean) {
        startActivity(android.content.Intent(this, NoteActivity::class.java)
            .putExtra("vault", vault.path).putExtra("path", rel).putExtra("edit", edit))
    }

    // --- search (scoped to the deployment) ---

    private data class Hit(val rel: String, val title: String, val snippet: String, val score: Int)

    private fun run(q: String) {
        val mine = ++seq
        val terms = q.lowercase().split(Regex("\\s+")).filter { it.isNotEmpty() }
        Thread {
            val hits = ArrayList<Hit>()
            scopeDir.walkTopDown().forEach { f ->
                if (seq != mine) return@forEach
                if (!f.isFile || !f.name.endsWith(".md")) return@forEach
                if (f.path.contains("/.")) return@forEach
                val text = runCatching { f.readText() }.getOrNull() ?: return@forEach
                val title = titleOf(text, f)
                val hay = (title + "\n" + text).lowercase()
                if (terms.all { hay.contains(it) }) {
                    val titleHits = terms.count { title.lowercase().contains(it) }
                    hits.add(Hit(f.relativeTo(vault).path, title, snippet(text, terms), titleHits * 10 + 1))
                }
            }
            hits.sortWith(compareByDescending<Hit> { it.score }.thenBy { it.title.lowercase() })
            val top = hits.take(60)
            h.post { if (seq == mine) paint(top, hits.size) }
        }.start()
    }

    private fun paint(hits: List<Hit>, total: Int) {
        results.removeAllViews()
        status.text = if (hits.isEmpty()) "No notes match. Try fewer words, or the callsign as it is written in the notes." else
            "$total match${if (total == 1) "" else "es"}" + if (total > hits.size) " (showing ${hits.size})" else ""
        val d = resources.displayMetrics.density
        for (hit in hits) {
            val cell = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(0, (10 * d).toInt(), 0, (10 * d).toInt())
                isClickable = true
                setOnClickListener { openNote(hit.rel, false) }
            }
            cell.addView(TextView(this).apply { text = hit.title; textSize = 16f; setTextColor(green) })
            if (hit.snippet.isNotEmpty()) cell.addView(TextView(this).apply {
                text = hit.snippet; textSize = 13f; setTextColor(greyBlue)
            })
            results.addView(cell)
            results.addView(View(this).apply {
                setBackgroundColor(beige)
                layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 1)
            })
        }
    }

    private fun titleOf(text: String, f: File): String {
        if (text.startsWith("---")) {
            val end = text.indexOf("\n---", 3)
            if (end > 0) Regex("(?m)^title:\\s*(.+?)\\s*$").find(text.substring(0, end))?.let {
                return it.groupValues[1].trim().trim('"')
            }
        }
        return f.name.removeSuffix(".md")
    }

    private fun snippet(text: String, terms: List<String>): String {
        for (line in text.split("\n")) {
            val l = line.trim()
            if (l.isEmpty() || l.startsWith("---") || l.startsWith("#")) continue
            if (terms.any { l.lowercase().contains(it) }) return if (l.length > 120) l.take(117) + "..." else l
        }
        return ""
    }
}
