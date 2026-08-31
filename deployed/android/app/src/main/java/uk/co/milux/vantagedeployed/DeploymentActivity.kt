package uk.co.milux.vantagedeployed

import android.graphics.Typeface
import android.os.Bundle
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * Choose which deployment the app shows. The device keeps syncing every deployment it is
 * enrolled with; this only changes what the Vault Viewer, graph and capture display. "All
 * deployments" shows the whole synced vault.
 */
class DeploymentActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "Deployment"
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }
        root.addView(TextView(this).apply {
            text = "Show which deployment?"
            textSize = 22f
            setTypeface(typeface, Typeface.BOLD)
        })
        root.addView(TextView(this).apply {
            text = "This changes what you see, not what syncs. The device keeps every " +
                "deployment it is enrolled with up to date."
            textSize = 13f
        })
        val current = Prefs.viewLabel(this)
        fun option(label: String, shown: String) {
            root.addView(Button(this).apply {
                text = if (label == current) "✓  $shown" else shown
                isAllCaps = false
                setOnClickListener {
                    Prefs.setViewLabel(this@DeploymentActivity, label)
                    finish()
                }
            })
        }
        Prefs.enrolledLabels(this).forEach { option(it, it) }
        option(Prefs.ALL, "All deployments")
        setContentView(ScrollView(this).apply { addView(root) })
    }
}
