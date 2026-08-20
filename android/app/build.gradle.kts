plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.omcgoo.windowwatch"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.omcgoo.windowwatch"
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "1.1"
    }

    buildFeatures {
        compose = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.glance:glance-appwidget:1.1.1")
    implementation("androidx.core:core-ktx:1.13.1")
    // Arrives transitively via Glance, but pinned explicitly: the refresh schedule is
    // load-bearing now, so it should not move because a Glance upgrade moved it.
    implementation("androidx.work:work-runtime-ktx:2.9.1")
}
