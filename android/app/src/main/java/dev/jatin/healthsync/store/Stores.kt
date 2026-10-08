package dev.jatin.healthsync.store

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import dev.jatin.healthsync.sync.StateStore
import dev.jatin.healthsync.sync.SyncState
import kotlinx.coroutines.flow.first
import kotlinx.serialization.json.Json
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** One DataStore file for everything; excluded from backup and device transfer. */
private val Context.dataStore by preferencesDataStore(name = "healthsync")

private val STATE = stringPreferencesKey("sync_state")
private val SECRET = stringPreferencesKey("ingest_secret") // base64(iv) + ":" + base64(ciphertext)
private val URL_OVERRIDE = stringPreferencesKey("url_override") // debug builds only

private val StateJson = Json { ignoreUnknownKeys = true; encodeDefaults = true }

/** Sync state as a single serialized value: every transition is one atomic edit. */
class DataStoreStateStore(private val context: Context) : StateStore {
    override suspend fun read(): SyncState = decode(context.dataStore.data.first()[STATE])

    override suspend fun update(transform: (SyncState) -> SyncState): SyncState {
        var result: SyncState? = null
        context.dataStore.edit { prefs ->
            val next = transform(decode(prefs[STATE]))
            prefs[STATE] = StateJson.encodeToString(SyncState.serializer(), next)
            result = next
        }
        return result!!
    }

    suspend fun hasProgress(): Boolean = context.dataStore.data.first()[STATE] != null

    private fun decode(raw: String?): SyncState =
        raw?.let { runCatching { StateJson.decodeFromString(SyncState.serializer(), it) }.getOrNull() } ?: SyncState()
}

/**
 * The phone ingest secret, encrypted with a non-exportable Android Keystore AES-GCM key.
 * Keystore keys are never backed up, so a restored install cannot decrypt it.
 */
class SecretStore(private val context: Context) {
    private val alias = "healthsync_ingest_secret_v1"

    private fun key(create: Boolean): SecretKey? {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(alias, null) as? SecretKey)?.let { return it }
        if (!create) return null
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return gen.generateKey()
    }

    suspend fun save(secret: String) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key(create = true)) }
        val ct = cipher.doFinal(secret.toByteArray())
        val encoded = Base64.encodeToString(cipher.iv, Base64.NO_WRAP) + ":" + Base64.encodeToString(ct, Base64.NO_WRAP)
        context.dataStore.edit { it[SECRET] = encoded }
    }

    suspend fun isPresent(): Boolean = context.dataStore.data.first()[SECRET] != null

    /** Null if absent or undecryptable (e.g. restored onto a new device). */
    suspend fun load(): String? {
        val raw = context.dataStore.data.first()[SECRET] ?: return null
        return runCatching {
            val (iv, ct) = raw.split(":").map { Base64.decode(it, Base64.NO_WRAP) }
            val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
                init(Cipher.DECRYPT_MODE, key(create = false) ?: error("no key"), GCMParameterSpec(128, iv))
            }
            String(cipher.doFinal(ct))
        }.getOrNull()
    }

    suspend fun clear() {
        context.dataStore.edit { it.remove(SECRET) }
    }

    suspend fun urlOverride(): String? = context.dataStore.data.first()[URL_OVERRIDE]
    suspend fun setUrlOverride(url: String?) {
        context.dataStore.edit { if (url.isNullOrBlank()) it.remove(URL_OVERRIDE) else it[URL_OVERRIDE] = url }
    }
}
