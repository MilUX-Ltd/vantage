// Ported from milux-vault-sync android/brain NoteActivity.kt at 42a89de (ADR-001); package renamed,
// vault rebased on the product's synced app-private store.
package uk.co.milux.vantagedeployed

import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.method.LinkMovementMethod
import android.text.style.ClickableSpan
import android.text.style.ForegroundColorSpan
import android.text.style.RelativeSizeSpan
import android.text.style.StyleSpan
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * View and edit one vault note offline. In read mode it renders the note the way Obsidian would,
 * with [[wikilinks]] tappable so you can walk the graph by reading. Edit turns it into the raw
 * markdown; Save writes it back to the vault file atomically. The note lives in the synced
 * app-private vault, so a saved edit reaches the box on the next Sync, based on the hash it was
 * received at (the box fast-forwards or stages a conflict honestly).
 *
 * Reached from the knowledge graph (tap a node, then Open note), from a wikilink, or from the Vault
 * Viewer. A new note is opened straight into edit mode.
 */
class NoteActivity : AppCompatActivity() {
    private lateinit var vaultRoot: File
    private lateinit var file: File
    private var rawText: String = ""
    private var readable = false
    private var editing = false

    private lateinit var scroll: ScrollView
    private lateinit var bodyView: TextView
    private lateinit var editor: EditText

    private val green = Color.parseColor("#113308")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        vaultRoot = File(intent.getStringExtra("vault") ?: Prefs.vault(this))
        file = File(vaultRoot, intent.getStringExtra("path") ?: "")
        val d = resources.displayMetrics.density
        val pad = (16 * d).toInt()

        bodyView = TextView(this).apply {
            textSize = 16f; setTextColor(green)
            setPadding(pad, pad, pad, pad); setTextIsSelectable(true)
            movementMethod = LinkMovementMethod.getInstance()
        }
        editor = EditText(this).apply {
            textSize = 15f; setTextColor(green)
            setPadding(pad, pad, pad, pad); gravity = android.view.Gravity.TOP
            isVerticalScrollBarEnabled = true
            typeface = Typeface.MONOSPACE
            visibility = View.GONE
        }
        scroll = ScrollView(this).apply { addView(bodyView) }

        val frame = FrameLayout(this)
        frame.addView(scroll, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        frame.addView(editor, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        setContentView(frame)

        val text = runCatching { file.readText() }.getOrNull()
        if (text == null) {
            readable = false
            bodyView.text = "Could not open ${file.name}. It is not readable on this phone. Go back and try again."
        } else {
            readable = true
            rawText = text
            title = titleOf(rawText, file)
            bodyView.text = render(stripFrontmatter(rawText))
            if (intent.getBooleanExtra("edit", false)) startEditing()
        }
    }

    // --- action bar: Edit in read mode; Save and Cancel in edit mode ---

    override fun onCreateOptionsMenu(menu: Menu): Boolean = true

    override fun onPrepareOptionsMenu(menu: Menu): Boolean {
        menu.clear()
        if (!readable) return true
        if (editing) {
            menu.add(0, ID_SAVE, 0, "Save").setShowAsAction(MenuItem.SHOW_AS_ACTION_ALWAYS)
            menu.add(0, ID_CANCEL, 1, "Cancel").setShowAsAction(MenuItem.SHOW_AS_ACTION_ALWAYS)
        } else {
            menu.add(0, ID_EDIT, 0, "Edit").setShowAsAction(MenuItem.SHOW_AS_ACTION_ALWAYS)
            menu.add(0, ID_DELETE, 1, "Delete").setShowAsAction(MenuItem.SHOW_AS_ACTION_NEVER)
        }
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        when (item.itemId) {
            ID_EDIT -> startEditing()
            ID_DELETE -> confirmDelete()
            ID_SAVE -> save()
            ID_CANCEL -> onBackPressed()
            else -> return super.onOptionsItemSelected(item)
        }
        return true
    }

    private fun confirmDelete() {
        val rel = file.relativeTo(vaultRoot).path
        androidx.appcompat.app.AlertDialog.Builder(this)
            .setTitle("Delete ${file.name}?")
            .setMessage("This removes the note from this phone, and from the box on the next " +
                "sync. If the box has changed it since you last synced, its version is kept " +
                "and the note returns. The removed version is retained in the box's history.")
            .setPositiveButton("Delete") { _, _ ->
                SyncEngine.deleteNote(this, rel)
                Toast.makeText(this, "Deleted. It is removed from the box on the next sync.",
                    Toast.LENGTH_SHORT).show()
                finish()
            }
            .setNegativeButton("Cancel", null).show()
    }

    private fun startEditing() {
        editing = true
        editor.setText(rawText)
        scroll.visibility = View.GONE
        editor.visibility = View.VISIBLE
        editor.requestFocus()
        invalidateOptionsMenu()
    }

    private fun cancelEditing() {
        editing = false
        editor.visibility = View.GONE
        scroll.visibility = View.VISIBLE
        invalidateOptionsMenu()
    }

    private fun save() {
        val text = editor.text.toString()
        val ok = runCatching { atomicWrite(file, text) }.isSuccess
        if (!ok) {
            Toast.makeText(this, "Not saved. ${file.name} could not be written. Copy your text before you leave this screen, then try again.", Toast.LENGTH_LONG).show()
            return
        }
        rawText = text
        title = titleOf(rawText, file)
        bodyView.text = render(stripFrontmatter(rawText))
        cancelEditing()
        Toast.makeText(this, "Saved on this phone. It reaches the box on the next Sync.", Toast.LENGTH_SHORT).show()
    }

    /** Write via a temp file then an atomic rename, so a kill mid-write cannot truncate the note. */
    private fun atomicWrite(target: File, text: String) {
        val tmp = File(target.parentFile, ".${target.name}.tmp")
        tmp.writeText(text)
        try {
            Files.move(tmp.toPath(), target.toPath(),
                StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
        } catch (e: Exception) {
            target.writeText(text)
            tmp.delete()
        }
    }

    override fun onBackPressed() {
        if (editing) {
            // Leaving edit mode can lose typed work: ask first when the text has changed.
            if (editor.text.toString() != rawText) {
                androidx.appcompat.app.AlertDialog.Builder(this)
                    .setTitle("Discard changes?")
                    .setMessage("Your edits to ${file.name} have not been saved.")
                    .setPositiveButton("Discard") { _, _ -> cancelEditing() }
                    .setNegativeButton("Keep editing", null).show()
            } else cancelEditing()
            return
        }
        super.onBackPressed()
    }

    override fun onResume() {
        super.onResume()
        // The note may have changed underneath us (an AI update saved, a sync arrived): re-read,
        // so the operator is never shown a superseded version as if it were current.
        if (!editing && readable) {
            val fresh = runCatching { file.readText() }.getOrNull()
            if (fresh != null && fresh != rawText) {
                rawText = fresh
                title = titleOf(rawText, file)
                bodyView.text = render(stripFrontmatter(rawText))
            }
        }
    }

    // --- rendering (read mode) ---

    private fun titleOf(text: String, file: File): String {
        Regex("(?m)^title:\\s*(.+?)\\s*$").find(frontmatter(text))?.let {
            return it.groupValues[1].trim().trim('"')
        }
        return file.name.removeSuffix(".md")
    }

    private fun frontmatter(text: String): String {
        if (!text.startsWith("---")) return ""
        val end = text.indexOf("\n---", 3)
        return if (end < 0) "" else text.substring(0, end)
    }

    private fun stripFrontmatter(text: String): String {
        if (!text.startsWith("---")) return text
        val end = text.indexOf("\n---", 3)
        if (end < 0) return text
        val after = text.indexOf('\n', end + 1)
        return if (after < 0) "" else text.substring(after + 1)
    }

    private fun render(md: String): CharSequence {
        val out = SpannableStringBuilder()
        for (raw in md.split("\n")) {
            val start = out.length
            val heading = Regex("^(#{1,6})\\s+(.*)$").find(raw)
            val line = if (heading != null) heading.groupValues[2] else raw
            appendWithLinks(out, dropInline(line))
            out.append("\n")
            if (heading != null) {
                out.setSpan(StyleSpan(Typeface.BOLD), start, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                val size = if (heading.groupValues[1].length <= 2) 1.35f else 1.15f
                out.setSpan(RelativeSizeSpan(size), start, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
        }
        return out
    }

    private fun dropInline(s: String): String =
        s.replace(Regex("\\*\\*(.+?)\\*\\*"), "$1")
            .replace(Regex("(?<!\\*)\\*(?!\\*)(.+?)(?<!\\*)\\*(?!\\*)"), "$1")
            .replace(Regex("^\\s*[-*+]\\s+"), "• ")

    private fun appendWithLinks(out: SpannableStringBuilder, line: String) {
        val re = Regex("\\[\\[([^\\]]+)\\]\\]")
        var i = 0
        for (m in re.findAll(line)) {
            out.append(line.substring(i, m.range.first))
            val inner = m.groupValues[1]
            val target = inner.substringBefore('|').substringBefore('#').trim()
            val shown = inner.substringAfter('|', inner.substringBefore('#')).trim()
            val at = out.length
            out.append(shown)
            out.setSpan(object : ClickableSpan() {
                override fun onClick(w: View) = openLink(target)
            }, at, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            out.setSpan(ForegroundColorSpan(Color.parseColor("#7A7433")), at, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            i = m.range.last + 1
        }
        out.append(line.substring(i))
    }

    private fun openLink(target: String) {
        Thread {
            val rel = resolve(target)
            runOnUiThread {
                if (rel == null) {
                    Toast.makeText(this, "No note called $target on this phone. It may not have synced yet.",
                        Toast.LENGTH_SHORT).show()
                } else {
                    startActivity(android.content.Intent(this, NoteActivity::class.java)
                        .putExtra("vault", vaultRoot.path).putExtra("path", rel))
                }
            }
        }.start()
    }

    private fun resolve(target: String): String? {
        val want = target.removeSuffix(".md")
        var titleHit: String? = null
        vaultRoot.walkTopDown().forEach { f ->
            if (!f.isFile || !f.name.endsWith(".md")) return@forEach
            if (f.name.removeSuffix(".md").equals(want, ignoreCase = true)) {
                return f.relativeTo(vaultRoot).path
            }
            if (titleHit == null) {
                val t = runCatching { frontmatter(f.readText()) }.getOrDefault("")
                if (Regex("(?m)^title:\\s*(.+?)\\s*$").find(t)?.groupValues?.get(1)
                        ?.trim()?.trim('"').equals(want, ignoreCase = true)) {
                    titleHit = f.relativeTo(vaultRoot).path
                }
            }
        }
        return titleHit
    }

    companion object {
        private const val ID_EDIT = 1
        private const val ID_DELETE = 5
        private const val ID_SAVE = 2
        private const val ID_CANCEL = 3
    }
}
