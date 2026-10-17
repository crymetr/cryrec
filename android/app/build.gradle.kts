import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// One version for the whole repo: read it from the desktop app.
val appVersion: String = Regex("""^VERSION = "(.+)"""", RegexOption.MULTILINE)
    .find(rootDir.resolve("../cryrec.pyw").readText())!!.groupValues[1]
val (vMajor, vMinor, vPatch) = appVersion.split(".").map { it.toInt() }

// Release signing: android/keystore.properties locally, CRYREC_KEYSTORE_* env vars in CI.
val ks = Properties().apply {
    val f = rootDir.resolve("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
fun ksValue(name: String, env: String): String? = ks.getProperty(name) ?: System.getenv(env)

android {
    namespace = "com.crymetr.cryrec"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.crymetr.cryrec"
        minSdk = 29
        targetSdk = 36
        versionName = appVersion
        versionCode = vMajor * 10000 + vMinor * 100 + vPatch
    }

    signingConfigs {
        create("release") {
            ksValue("storeFile", "CRYREC_KEYSTORE_FILE")?.let { storeFile = file(it) }
            storePassword = ksValue("storePassword", "CRYREC_KEYSTORE_PASSWORD")
            keyAlias = ksValue("keyAlias", "CRYREC_KEY_ALIAS")
            keyPassword = ksValue("keyPassword", "CRYREC_KEYSTORE_PASSWORD")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
            if (ksValue("storeFile", "CRYREC_KEYSTORE_FILE") != null) signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}
