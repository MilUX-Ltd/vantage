package uk.co.milux.vantagedeployed

import android.graphics.Typeface
import android.os.Bundle
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity

/**
 * Resolve sync conflicts (the piece deferred with intent since the estate build). A conflict
 * means the box and this phone both changed one note between syncs; the vault kept the local
 * version and the box's version was staged OUTSIDE the vault. Here the operator sees both and
 * chooses: keep mine (my edit wins, pushed to the box next sync), or take theirs (the box's
 * version replaces mine). The vault never holds two copies of a note; only the resolution
 * enters it.
 */
class ConflictsActivity : AppCompatActivity() {
    private lateinit var listView: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "Conflicts"
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }
        root.addView(TextView(this).apply {
            text = "Conflicts"
            textSize = 24f
            setTypeface(typeface, Typeface.BOLD)
        })
        root.addView(TextView(this).apply {
            text = "The box and this phone both changed the same note. Choose which version " +
                "stands; the other is discarded. The next sync settles it."
            textSize = 13f
        })
        listView = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(listView)
        setContentView(ScrollView(this).apply { addView(root) })
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    private fun render() {
        listView.removeAllViews()
        val conflicts = SyncEngine.conflicts(this)
        if (conflicts.isEmpty()) {
            listView.addView(TextView(this).apply {
                text = "\nNothing to resolve. Conflicts appear here after a sync when an edit " +
                    "clashed with the box."
                textSize = 15f
            })
            return
        }
        conflicts.forEach { k ->
            listView.addView(TextView(this).apply {
                text = "\n${k.rel}"
                textSize = 16f
                setTypeface(Typeface.MONOSPACE, Typeface.BOLD)
            })
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            row.addView(Button(this).apply {
                text = "View both"
                isAllCaps = false
                setOnClickListener { viewBoth(k) }
            })
            row.addView(Button(this).apply {
                text = "Keep mine"
                isAllCaps = false
                setOnClickListener {
                    SyncEngine.resolveKeepMine(this@ConflictsActivity, k)
                    Toast.makeText(this@ConflictsActivity,
                        "Kept your version; it reaches the box on the next sync.",
                        Toast.LENGTH_SHORT).show()
                    render()
                }
            })
            row.addView(Button(this).apply {
                text = "Take theirs"
                isAllCaps = false
                setOnClickListener {
                    SyncEngine.resolveTakeTheirs(this@ConflictsActivity, k)
                    Toast.makeText(this@ConflictsActivity,
                        "Took the box's version.", Toast.LENGTH_SHORT).show()
                    render()
                }
            })
            listView.addView(row)
        }
    }

    private fun viewBoth(k: SyncEngine.Conflict) {
        val mine = java.io.File(SyncEngine.vaultRoot(this), k.rel)
            .let { if (it.exists()) it.readText() else "(the local note is gone)" }
        val theirs = k.staged.readText()
        AlertDialog.Builder(this)
            .setTitle(k.rel.substringAfterLast('/'))
            .setMessage("YOURS (kept in the vault):\n\n$mine\n\n" +
                "- - - - - -\n\nTHEIRS (from the box):\n\n$theirs")
            .setPositiveButton("Close", null)
            .show()
    }
}
