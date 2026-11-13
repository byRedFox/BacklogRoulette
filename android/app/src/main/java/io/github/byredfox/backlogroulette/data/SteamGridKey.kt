package io.github.byredfox.backlogroulette.data

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Ключ SteamGridDB пользователя. Хранится только на телефоне, в файле steamgriddb.key во внутренней папке,
 * зашифрованным (AES-GCM) ключом из хранилища ключей Android: сам ключ шифрования из хранилища не достать, а
 * копий приложения нет (allowBackup=false). В журнал, экспорт и на экран целиком ключ не попадает.
 */
object SteamGridKey {
    private const val ALIAS = "backlogroulette_steamgriddb"
    private const val TRANSFORMATION = "AES/GCM/NoPadding"
    private const val TAG_BITS = 128
    /** Ключ — только буквы и цифры: ничего лишнего не попадёт в заголовок запроса. */
    val VALID = Regex("[A-Za-z0-9]{16,128}")

    private var file: File? = null
    @Volatile private var key: String? = null
    var saved by mutableStateOf(false)
        private set
    var tail by mutableStateOf("") // последние 4 знака — узнать, какой ключ сохранён
        private set

    fun init(filesDir: File) {
        if (file != null) return
        val target = File(filesDir, "steamgriddb.key")
        file = target
        if (!target.exists()) return
        try {
            keep(decrypt(target.readBytes()))
        } catch (e: Exception) {
            AppLog.warning("Ключ SteamGridDB не расшифровался — введите его снова (${e.javaClass.simpleName})")
            target.delete()
        }
    }

    fun get(): String? = key

    fun save(value: String) {
        val text = value.trim()
        require(VALID.matches(text)) { "ключ — только латинские буквы и цифры" }
        val target = file ?: throw IllegalStateException("хранилище не готово")
        val tmp = File(target.parentFile, target.name + ".tmp")
        tmp.writeBytes(encrypt(text))
        if (!tmp.renameTo(target)) {
            target.delete()
            if (!tmp.renameTo(target)) throw java.io.IOException("не удалось записать ключ")
        }
        keep(text)
        AppLog.info("SteamGridDB: ключ сохранён")
    }

    fun clear() {
        file?.delete()
        try {
            KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.deleteEntry(ALIAS)
        } catch (e: Exception) {
            AppLog.warning("Ключ шифрования не удалился: ${e.javaClass.simpleName}")
        }
        key = null
        saved = false
        tail = ""
        AppLog.info("SteamGridDB: ключ удалён")
    }

    private fun keep(value: String) {
        key = value
        saved = true
        tail = value.takeLast(4)
    }

    private fun secretKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return generator.generateKey()
    }

    // Файл: длина IV (1 байт), IV, зашифрованный ключ с меткой подлинности
    private fun encrypt(text: String): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val iv = cipher.iv
        return byteArrayOf(iv.size.toByte()) + iv + cipher.doFinal(text.toByteArray(Charsets.UTF_8))
    }

    private fun decrypt(bytes: ByteArray): String {
        val ivSize = bytes[0].toInt()
        require(ivSize in 12..16 && bytes.size > 1 + ivSize) { "файл повреждён" }
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(TAG_BITS, bytes, 1, ivSize))
        val text = cipher.doFinal(bytes, 1 + ivSize, bytes.size - 1 - ivSize).toString(Charsets.UTF_8)
        require(VALID.matches(text)) { "не ключ" }
        return text
    }
}
