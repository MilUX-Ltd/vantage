pluginManagement {
    repositories { google(); mavenCentral(); gradlePluginPortal() }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories { google(); mavenCentral() }
}
rootProject.name = "VantageDeployed"
// Canonical request signing (uk.co.milux.shared:identity) comes from the sibling repository as
// a composite build (ADR-001 decision 6). The default path fits the canonical checkout layout
// (code/milux-tak/vantage-deployed/android beside code/milux-eud-shared); a worktree build
// passes -PeudShared=/absolute/path/to/milux-eud-shared instead.
includeBuild(providers.gradleProperty("eudShared").getOrElse("../eud-shared"))
include(":app")
