package uk.co.milux.vantagedeployed

import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import java.io.File

/**
 * Mission and map packs pulled for this deployment (Spec 005), each a tap away from ATAK.
 * The honest hand-off (ADR-003): ATAK imports its own data packages, so the pack goes
 * through the platform share sheet; read access is granted on the packs directory only.
 */
class PacksActivity : AppCompatActivity() {
    private lateinit var listView: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "Packs"
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }
        root.addView(TextView(this).apply {
            text = "Packs"
            textSize = 24f
            setTypeface(typeface, Typeface.BOLD)
        })
        root.addView(TextView(this).apply {
            text = "Assigned to this deployment by the operator. Tap one to send it to ATAK."
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
        val index = SyncEngine.packsIndex(this)
        if (index.isEmpty()) {
            listView.addView(TextView(this).apply {
                text = "\nNo packs yet. Sync from the home screen to bring them down."
                textSize = 15f
            })
            return
        }
        index.forEach { e ->
            val name = e["name"] ?: return@forEach
            val file = File(SyncEngine.packsRoot(this), name)
            val kb = ((e["size"]?.toLongOrNull() ?: 0L) + 512) / 1024
            listView.addView(Button(this).apply {
                text = "$name  (${e["kind"]}, ${kb} KB)" +
                    if (!file.exists()) "  [not pulled yet]" else ""
                isAllCaps = false
                isEnabled = file.exists()
                setOnClickListener { sendToAtak(file) }
            })
        }
    }

    private fun sendToAtak(file: File) {
        val uri = FileProvider.getUriForFile(this,
            "uk.co.milux.vantagedeployed.fileprovider", file)
        startActivity(Intent.createChooser(
            Intent(Intent.ACTION_SEND)
                .setType("application/zip")
                .putExtra(Intent.EXTRA_STREAM, uri)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION),
            "Send ${file.name}"))
    }
}
