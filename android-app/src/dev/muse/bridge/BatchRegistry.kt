package dev.muse.bridge

import java.io.BufferedReader
import java.io.File
import java.io.IOException
import java.io.InputStreamReader
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.NoSuchFileException
import org.json.JSONArray
import org.json.JSONObject

/**
 * SPEC-BATCH-PERSIST-001 v2.8 (C-01, Decision 103): persistent batch
 * execution records.
 *
 * The BatchRegistry is the SOLE authority for batch identity, state,
 * and persistence. The old in-memory batchCache in
 * BridgeAccessibilityService is gone: there is no dual management.
 * The Registry owns the in-memory record map, the persistent file,
 * the single lock, the startup classification, and the capacity
 * policy.
 *
 * Design (see ~/workspace/research/c01-implementation-plan-2026-11-13.md):
 * every mutation builds a copy of the store content, persists it
 * with the atomic write (temp file, flush, fsync, rename, directory
 * fsync — BridgeLog.atomicWriteText), and only then commits the
 * copy to memory. A failed persist never changes memory.
 *
 * The phone-side durability gate (SPEC §2.2, C-01-18) is ON until
 * the real-device power-loss-equivalent test demonstrates the
 * storage method and the marker is recorded: /batch is refused
 * with 503 batch_store_durability_unverified until then. That is
 * the SPEC's requirement, not a bug.
 */

/** 503 with a SPEC error id (persist_failed, batch_store_unavailable,
 * batch_store_full, batch_store_recovery_pending,
 * batch_store_durability_unverified). */
class BatchStoreException(val errorId: String, message: String) : Exception(message)

/** 400: a malformed request (bad batch_id, bad supersedes, a confirm
 * on a record that is not UNKNOWN, a recovery completion when no
 * recovery is pending, an unknown storage method). */
class BatchBadRequestException(message: String) : Exception(message)

/** 404: a management operation named a batch_id with no record. */
class BatchNotFoundException(message: String) : Exception(message)

/** The outcome of an admission check (SPEC §2.3). */
enum class AdmitKind {
    /** No record: persist IN_PROGRESS, then execute. */
    NEW,
    /** A terminal record with a valid result: replay it. */
    REPLAY,
    /** Still running in this process: report "running". */
    RUNNING,
    /** The outcome is unknown: report "unknown", never re-execute. */
    UNKNOWN
}

/** What admit() decided. `result` is set only for REPLAY (a copy of
 * the stored result, with replayed=true). */
data class AdmitDecision(val kind: AdmitKind, val result: BridgeAccessibilityService.BatchResult?)

/** What restoreFromBackup() did (for the management response). */
data class RestoreSummary(val restoredRecords: Int, val convertedToUnknown: Int)

/** One manual confirmation, recorded on an UNKNOWN record (SPEC §2.6).
 * The record stays UNKNOWN; this is an annotation, not a transition.
 * confirmedAt is set by the server; confirmedBy is the human who
 * checked the phone (an attestation, not an authenticated identity);
 * confirmedVia is the authenticated subject that executed the
 * management operation. Public because confirm() returns it to the
 * server for the management response. */
data class ManualConfirmation(
    val confirmedAt: Long,
    val confirmedBy: String,
    val confirmedVia: String,
    val outcome: String,
    val note: String
)

/** One batch record (SPEC §2.2). Immutable: every change builds a
 * new copy, so a shallow map copy is safe. `result` is null while
 * IN_PROGRESS and while UNKNOWN. */
private data class BatchRecord(
    val batchId: String,
    val payloadHash: String,
    val status: String,
    val result: BridgeAccessibilityService.BatchResult?,
    val createdAt: Long,
    val expiresAt: Long,
    val tombstoneUntil: Long,
    val unknownAt: Long?,
    val manualConfirmation: ManualConfirmation?,
    val supersedes: String?
)

/** The whole store content (SPEC §2.2 top level). */
private data class StoreContent(
    val records: LinkedHashMap<String, BatchRecord>,
    val recoveryPending: Boolean,
    val recoveryInfo: JSONObject?,
    val durabilityVerified: JSONObject?
)

class BatchRegistry(private val dir: File) {

    companion object {
        /** Result lifetime (SPEC §2.7, unchanged from BATCH_TTL_MS). */
        const val RESULT_TTL_MS: Long = 10 * 60 * 1000
        /** Tombstone after the result expires (unchanged from
         * BATCH_TOMBSTONE_MS). */
        const val TOMBSTONE_MS: Long = 10 * 60 * 1000
        /** UNKNOWN records are kept a further 24 h (SPEC §2.7). */
        const val UNKNOWN_RETENTION_MS: Long = 24 * 60 * 60 * 1000
        const val MAX_RECORDS: Int = 100
        const val MAX_FILE_BYTES: Int = 512 * 1024
        const val MAX_RECORD_BYTES: Int = 64 * 1024
        const val MAX_BATCH_ID_LEN: Int = 128
        const val STORE_FILE: String = "batch_records.json"
        const val BAK_FILE: String = "batch_records.json.bak"
        /** The storage method this build uses (SPEC §2.2). The
         * durability marker must name this method; if the method
         * ever changes, re-verification is required before /batch
         * resumes. */
        const val STORAGE_METHOD: String = "file_atomic_v1"
        private val TERMINAL_STATUSES = setOf("COMPLETED", "PARTIAL", "FAILED")
    }

    /** The single Registry lock (SPEC §2.3). It serializes the whole
     * admission unit: ID lookup, payload-hash compare, new-record
     * registration, and the IN_PROGRESS persist. Execution itself
     * runs outside this lock. */
    private val lock = Any()

    private enum class StoreState { NORMAL, EMPTY, CORRUPT }

    private var initialized = false
    private var storeState = StoreState.EMPTY
    private var content = StoreContent(LinkedHashMap(), false, null, null)
    /** The last content successfully written to the live file (for
     * the .bak, which is at most one persist behind). Null when no
     * live file has ever been written. */
    private var lastPersistedJson: String? = null

    /**
     * The UNKNOWN tombstone (SPEC §2.2 / §2.7): a full 24 h from
     * the UNKNOWN transition, and at least the original 20 min +
     * 24 h from creation — whichever is later.
     */
    private fun unknownTombstoneUntil(createdAt: Long, unknownAt: Long): Long {
        return maxOf(createdAt + RESULT_TTL_MS + TOMBSTONE_MS, unknownAt) + UNKNOWN_RETENTION_MS
    }

    /**
     * Convert a record to UNKNOWN (SPEC §2.7): unknownAt is set
     * and the tombstone is recomputed to the UNKNOWN formula. An
     * existing longer tombstone is never shortened (load-time
     * migration for records written by the pre-fix build, whose
     * tombstone was still createdAt + 20 min — CONS-152 P0-01).
     */
    private fun toUnknown(rec: BatchRecord, unknownAt: Long): BatchRecord {
        return rec.copy(
            status = "UNKNOWN",
            result = null,
            unknownAt = unknownAt,
            tombstoneUntil = maxOf(
                rec.tombstoneUntil,
                unknownTombstoneUntil(rec.createdAt, unknownAt)
            )
        )
    }

    /**
     * Fail-closed guard for management operations that must not
     * run on a corrupt store (CONS-152 P0-02). The recovery paths
     * (restoreFromBackup, clearStore) do NOT use this guard — they
     * are how a corrupt store is recovered. Caller must hold the
     * lock.
     */
    private fun requireNotCorruptLocked() {
        if (storeState == StoreState.CORRUPT) {
            throw BatchStoreException("batch_store_unavailable",
                "batch store is corrupt or unreadable; /batch is unavailable until an operator restores it")
        }
    }

    /**
     * Load and classify the store (SPEC §2.5). Called once by the
     * accessibility service at startup. Never throws: a corrupt or
     * unreadable file is a state (CORRUPT, /batch fail-closed), not
     * an exception. IN_PROGRESS records become UNKNOWN (their
     * outcome is unknown because the previous process did not
     * finish them); the conversion is persisted when it can be.
     */
    fun init() {
        synchronized(lock) {
            if (initialized) return
            initialized = true
            // A leftover temp file is ignored and deleted; it is
            // never treated as the record (SPEC §2.2).
            File(dir, STORE_FILE + ".tmp").delete()
            File(dir, BAK_FILE + ".tmp").delete()
            val text: String?
            try {
                text = readFileText(File(dir, STORE_FILE))
            } catch (e: Exception) {
                // The file exists but cannot be read: CORRUPT,
                // /batch fail-closed (SPEC §2.5). Never treated
                // as an empty store (CONS-152 P1-01).
                storeState = StoreState.CORRUPT
                content = StoreContent(LinkedHashMap(), false, null, null)
                lastPersistedJson = null
                BridgeLog.log("batch_registry", "store unreadable: " + e.message)
                return
            }
            if (text == null) {
                // No file: the store was never initialized (a fresh
                // install). Normal empty.
                storeState = StoreState.EMPTY
                content = StoreContent(LinkedHashMap(), false, null, null)
                lastPersistedJson = null
                return
            }
            val parsed: StoreContent
            try {
                checkStoreSize(text)
                parsed = storeFromJson(JSONObject(text))
            } catch (e: Exception) {
                // Corrupt / unreadable: /batch is fail-closed. A
                // corrupt record is never treated as empty.
                storeState = StoreState.CORRUPT
                content = StoreContent(LinkedHashMap(), false, null, null)
                lastPersistedJson = null
                BridgeLog.log("batch_registry", "store corrupt: " + e.message)
                return
            }
            lastPersistedJson = text
            storeState = if (parsed.records.isEmpty() && !parsed.recoveryPending) {
                StoreState.EMPTY
            } else {
                StoreState.NORMAL
            }
            val now = System.currentTimeMillis()
            var converted = 0
            var migrated = 0
            val records = LinkedHashMap<String, BatchRecord>()
            for ((id, rec) in parsed.records) {
                if (rec.status == "IN_PROGRESS") {
                    records[id] = toUnknown(rec, now)
                    converted++
                } else if (rec.status == "UNKNOWN") {
                    // Load-time migration (CONS-152 P0-01): a
                    // record written by the pre-fix build keeps
                    // the old tombstone (createdAt + 20 min).
                    // Recompute it; toUnknown() never shortens
                    // an existing longer tombstone.
                    val fixed = toUnknown(rec, rec.unknownAt ?: now)
                    if (fixed.tombstoneUntil != rec.tombstoneUntil) {
                        records[id] = fixed
                        migrated++
                    } else {
                        records[id] = rec
                    }
                } else {
                    records[id] = rec
                }
            }
            content = parsed.copy(records = records)
            if (converted > 0 || migrated > 0) {
                // Persist the conversion / migration. If it fails,
                // the file still says IN_PROGRESS and the next
                // restart converts again; the in-memory state is
                // UNKNOWN either way, so no re-execution is possible.
                if (!persistLocked(content, backupPrevious = true)) {
                    BridgeLog.log("batch_registry",
                        "could not persist IN_PROGRESS->UNKNOWN conversion for "
                            + converted + " record(s) / tombstone migration for "
                            + migrated + " record(s)")
                }
            }
        }
    }

    private fun ensureInitialized() {
        if (!initialized) init()
    }

    /**
     * Read a store file. Returns null only when absence is
     * positively confirmed (CONS-153 P1-01): Files.notExists()
     * is true — the ENOENT equivalent. File.exists() and
     * Files.exists() return false both when the file does not
     * exist AND when its existence cannot be determined, so
     * they must never decide "new store" on their own. An
     * undeterminable status, a permission error, or any other
     * I/O error throws: the caller must treat that as CORRUPT /
     * unreadable, never as an empty store. A NoSuchFileException
     * on open (the file was deleted between the check and the
     * open) is the ENOENT race and returns null — the file is
     * absent at read time.
     */
    private fun readFileText(file: File): String? {
        val path = file.toPath()
        try {
            if (Files.notExists(path)) return null
        } catch (e: SecurityException) {
            throw IOException(
                "cannot determine whether store file exists: " + file.name, e)
        }
        try {
            if (!Files.exists(path)) {
                throw IOException(
                    "cannot determine whether store file exists "
                        + "(undeterminable status): " + file.name)
            }
        } catch (e: SecurityException) {
            throw IOException(
                "cannot determine whether store file exists: " + file.name, e)
        }
        var reader: BufferedReader? = null
        try {
            reader = BufferedReader(
                InputStreamReader(Files.newInputStream(path), StandardCharsets.UTF_8))
            val sb = StringBuilder()
            val buf = CharArray(8192)
            while (true) {
                val n = reader.read(buf)
                if (n < 0) break
                sb.append(buf, 0, n)
            }
            return sb.toString()
        } catch (e: NoSuchFileException) {
            // Deleted between the existence check and the open:
            // absent at read time (ENOENT race).
            return null
        } finally {
            if (reader != null) {
                try {
                    reader.close()
                } catch (_: Exception) {
                }
            }
        }
    }

    /**
     * The admission unit (SPEC §2.3): gates first, then the ID
     * lookup, then (for a new batch) the IN_PROGRESS persist. The
     * whole unit runs under the Registry lock. Execution itself runs
     * outside the lock, after this returns NEW.
     *
     * The A-3 snapshot entry check (tree_hash vs the last snapshot)
     * is NOT here: the service does it before calling admit(), so a
     * stale batch never reaches the Registry and never creates a
     * record.
     *
     * @param supersedes the batch_id of the UNKNOWN record this
     * batch replaces, or null. Validated inside the lock (SPEC §2.6).
     * @return NEW (execute now), REPLAY (return the stored result),
     * RUNNING (still in flight), or UNKNOWN (never re-execute).
     * @throws BridgeAccessibilityService.BatchMismatchException (409)
     * @throws BridgeAccessibilityService.BatchGoneException (410)
     * @throws BatchStoreException (503, one of the SPEC error ids)
     * @throws BatchBadRequestException (400, malformed id/hash or
     * an invalid supersedes reference)
     */
    fun admit(
        batchId: String,
        payloadHash: String,
        supersedes: String?
    ): AdmitDecision {
        synchronized(lock) {
            ensureInitialized()
            // Gates first: while any gate is on, /batch is refused
            // for every request, including new batches.
            if (storeState == StoreState.CORRUPT) {
                throw BatchStoreException("batch_store_unavailable",
                    "batch store is corrupt or unreadable; /batch is unavailable until an operator restores it")
            }
            if (content.recoveryPending) {
                throw BatchStoreException("batch_store_recovery_pending",
                    "batch store recovery is pending operator verification; /batch is refused until it completes")
            }
            if (!isDurabilityVerifiedLocked()) {
                throw BatchStoreException("batch_store_durability_unverified",
                    "batch store durability is not yet demonstrated on this device; /batch is refused until the phone-side durability test passes")
            }
            // The server validates the batch_id format (SPEC §2.9):
            // non-empty, at most 128 chars. The payload hash is a
            // 64-char hex SHA-256.
            if (batchId.isEmpty() || batchId.length > MAX_BATCH_ID_LEN) {
                throw BatchBadRequestException("malformed batch_id")
            }
            if (payloadHash.length != 64
                || !payloadHash.all { it in '0'..'9' || it in 'a'..'f' }
            ) {
                throw BatchBadRequestException("malformed payload_hash")
            }
            val now = System.currentTimeMillis()
            val existing = content.records[batchId]
            if (existing != null) {
                if (existing.status == "IN_PROGRESS") {
                    // Never evicted while alive: a re-send gets
                    // "running" even past the tombstone (SPEC §2.7).
                    // A re-send with different content is a 409,
                    // not "running" (CONS-156 P2-1): the same
                    // batch_id must never name two payloads.
                    if (existing.payloadHash != payloadHash) {
                        throw BridgeAccessibilityService.BatchMismatchException(
                            "batch_id " + batchId + " is already in use with different content")
                    }
                    return AdmitDecision(AdmitKind.RUNNING, null)
                }
                if (existing.status == "UNKNOWN") {
                    // UNKNOWN is always answered UNKNOWN, before
                    // any expiry check (CONS-152 P0-01): it has no
                    // result to expire, and treating it as a new
                    // record would re-execute an operation whose
                    // outcome is unknown. Its tombstone is
                    // unknownAt + 24 h; the record is purged only
                    // lazily, when another new batch arrives.
                    if (existing.payloadHash != payloadHash) {
                        throw BridgeAccessibilityService.BatchMismatchException(
                            "batch_id " + batchId + " is already in use with different content")
                    }
                    return AdmitDecision(AdmitKind.UNKNOWN, null)
                }
                if (now > existing.tombstoneUntil) {
                    // Past the tombstone: fall through to the
                    // new-record path (the purge below drops it).
                } else if (now > existing.expiresAt) {
                    throw BridgeAccessibilityService.BatchGoneException(
                        "batch_id " + batchId + " expired — use a new batch_id")
                } else if (existing.payloadHash != payloadHash) {
                    throw BridgeAccessibilityService.BatchMismatchException(
                        "batch_id " + batchId + " is already in use with different content")
                } else {
                    // Terminal with a valid result: replay it
                    // (replayed: true), no re-execution. The copy
                    // constructor does not copy `replayed`, so the
                    // copy starts false and is set true here.
                    val stored = existing.result
                        ?: throw BatchStoreException("batch_store_unavailable",
                            "stored result missing for batch_id " + batchId)
                    val replay = BridgeAccessibilityService.BatchResult(stored)
                    replay.replayed = true
                    return AdmitDecision(AdmitKind.REPLAY, replay)
                }
            }
            // A new batch. The supersedes reference is validated
            // inside the lock (SPEC §2.6): it must name an existing
            // UNKNOWN record, and it must not be the new batch itself.
            // A re-operation is issued only after a manual
            // confirmation (a human checked the phone and approved
            // the new operation), so the target must already carry
            // a recorded manualConfirmation (CONS-152 P1-04).
            if (supersedes != null) {
                if (supersedes == batchId) {
                    throw BatchBadRequestException(
                        "supersedes must not reference the new batch itself")
                }
                val target = content.records[supersedes]
                if (target == null) {
                    throw BatchBadRequestException(
                        "supersedes references a nonexistent record: " + supersedes)
                }
                if (target.status != "UNKNOWN") {
                    throw BatchBadRequestException(
                        "supersedes must reference an UNKNOWN record: " + supersedes)
                }
                if (target.manualConfirmation == null) {
                    throw BatchBadRequestException(
                        "supersedes references an UNKNOWN record with no recorded manual confirmation: " + supersedes)
                }
            }
            val record = BatchRecord(
                batchId = batchId,
                payloadHash = payloadHash,
                status = "IN_PROGRESS",
                result = null,
                createdAt = now,
                expiresAt = now + RESULT_TTL_MS,
                tombstoneUntil = now + RESULT_TTL_MS + TOMBSTONE_MS,
                unknownAt = null,
                manualConfirmation = null,
                supersedes = supersedes
            )
            // A record larger than 64 KB is rejected at persist time
            // and treated as a persist failure (SPEC §2.2).
            if (recordJsonBytes(record) > MAX_RECORD_BYTES) {
                throw BatchStoreException("persist_failed",
                    "batch record exceeds 64 KB; batch was not executed")
            }
            // Capacity (SPEC §2.7): purge expired records first
            // (never a live IN_PROGRESS, never a live terminal
            // record), then refuse if still no room.
            val nextRecords = LinkedHashMap(content.records)
            for ((id, rec) in content.records) {
                if (rec.status != "IN_PROGRESS" && now > rec.tombstoneUntil) {
                    nextRecords.remove(id)
                }
            }
            nextRecords[batchId] = record
            val next = content.copy(records = nextRecords)
            if (nextRecords.size > MAX_RECORDS
                || storeToJson(next).toString().toByteArray(StandardCharsets.UTF_8).size > MAX_FILE_BYTES
            ) {
                throw BatchStoreException("batch_store_full",
                    "batch store is full and no safely-deletable record exists; batch was not executed")
            }
            // Step 2 of the fixed order (SPEC §2.3): persist
            // IN_PROGRESS before any execution. If the persist
            // fails, the batch is NOT executed (fail-closed).
            if (!persistLocked(next, backupPrevious = true)) {
                throw BatchStoreException("persist_failed",
                    "could not persist the IN_PROGRESS record; batch was not executed")
            }
            content = next
            return AdmitDecision(AdmitKind.NEW, null)
        }
    }

    /**
     * Step 4 of the fixed order (SPEC §2.3): persist the result
     * after execution. Called by the service after runBatchInternal
     * returns. The result's status must be terminal (completed,
     * partial, or failed).
     *
     * If the persist fails, the batch has already executed, so the
     * outcome is not "not executed": the record stays IN_PROGRESS
     * in the file and is marked UNKNOWN in memory (a re-send gets
     * "unknown", never a re-execution). The original caller still
     * gets its result — the service returns it regardless of this
     * call's outcome (SPEC §2.4).
     */
    fun complete(
        batchId: String,
        payloadHash: String,
        result: BridgeAccessibilityService.BatchResult
    ) {
        synchronized(lock) {
            ensureInitialized()
            val existing = content.records[batchId]
            if (existing == null) {
                BridgeLog.log("batch_registry", "complete: no record for " + batchId)
                return
            }
            if (existing.status != "IN_PROGRESS" || existing.payloadHash != payloadHash) {
                BridgeLog.log("batch_registry",
                    "complete: record not IN_PROGRESS for " + batchId)
                return
            }
            // BatchResult.status is the wire format (lowercase:
            // "completed" / "partial" / "failed"); the stored record
            // status is the SPEC §2.2 vocabulary (uppercase:
            // COMPLETED / PARTIAL / FAILED). The two were compared
            // directly, so every result failed this check and was
            // never persisted (live failure 2026-11-13, C-01-4: a
            // re-sent completed batch_id answered "running", and a
            // restart would convert every completed record to
            // UNKNOWN). Normalize at this boundary; the stored
            // `result` JSON keeps its lowercase status, identical
            // to the original HTTP response (§2.2).
            val recordStatus = result.status.uppercase()
            if (recordStatus !in TERMINAL_STATUSES) {
                BridgeLog.log("batch_registry",
                    "complete: non-terminal result status for " + batchId + ": " + result.status)
                return
            }
            val updated = existing.copy(status = recordStatus, result = result)
            if (recordJsonBytes(updated) > MAX_RECORD_BYTES) {
                // The completed record exceeds the cap: treat it as
                // a persist failure (SPEC §2.2) — see below.
                markUnknownInMemoryLocked(existing, "completed record exceeds 64 KB")
                return
            }
            val nextRecords = LinkedHashMap(content.records)
            nextRecords[batchId] = updated
            val next = content.copy(records = nextRecords)
            if (persistLocked(next, backupPrevious = true)) {
                content = next
            } else {
                markUnknownInMemoryLocked(existing, "result persist failed")
            }
        }
    }

    /** The result persist failed (SPEC §2.4): keep the record
     * IN_PROGRESS in the file, mark it UNKNOWN in memory so a
     * re-send gets "unknown". On the next restart the file's
     * IN_PROGRESS converts to UNKNOWN the same way. */
    private fun markUnknownInMemoryLocked(existing: BatchRecord, reason: String) {
        val unknown = toUnknown(existing, System.currentTimeMillis())
        val memRecords = LinkedHashMap(content.records)
        memRecords[existing.batchId] = unknown
        content = content.copy(records = memRecords)
        BridgeLog.log("batch_registry",
            "complete: " + reason + " for " + existing.batchId + "; marked UNKNOWN in memory")
    }

    /**
     * Record a manual confirmation on an UNKNOWN record (SPEC §2.6).
     * The record stays UNKNOWN; the confirmation is an annotation.
     * `confirmedAt` is set by the server at processing time;
     * `confirmedBy` is the human who checked the phone (an
     * attestation supplied through the client); `confirmedVia` is
     * the authenticated subject that executed this operation. If
     * the persist fails, the confirmation is NOT recorded and the
     * caller is told to retry.
     *
     * @return the recorded confirmation (for the response).
     * @throws BatchNotFoundException (404) when the id has no record
     * @throws BatchBadRequestException (400) when the record is not
     * UNKNOWN or the outcome is invalid
     * @throws BatchStoreException (503 persist_failed)
     */
    fun confirm(
        batchId: String,
        confirmedBy: String,
        confirmedVia: String,
        outcome: String,
        note: String
    ): ManualConfirmation {
        synchronized(lock) {
            ensureInitialized()
            requireNotCorruptLocked()
            val existing = content.records[batchId]
                ?: throw BatchNotFoundException("no record for batch_id " + batchId)
            if (existing.status != "UNKNOWN") {
                throw BatchBadRequestException(
                    "record for batch_id " + batchId + " is not UNKNOWN")
            }
            if (outcome != "executed" && outcome != "not_executed" && outcome != "partial") {
                throw BatchBadRequestException("invalid outcome: " + outcome)
            }
            val mc = ManualConfirmation(
                confirmedAt = System.currentTimeMillis(),
                confirmedBy = confirmedBy,
                confirmedVia = confirmedVia,
                outcome = outcome,
                note = note
            )
            val updated = existing.copy(manualConfirmation = mc)
            // The 64 KB per-record cap (CONS-156 P2-3): confirm()
            // must check the updated record before persistLocked(),
            // like admit() and complete() do. An oversize
            // confirmation is a persist failure, not a 400 — the
            // confirmation is not recorded.
            if (recordJsonBytes(updated) > MAX_RECORD_BYTES) {
                throw BatchStoreException("persist_failed",
                    "manual confirmation would make the batch record exceed 64 KB; confirmation was not recorded")
            }
            val nextRecords = LinkedHashMap(content.records)
            nextRecords[batchId] = updated
            val next = content.copy(records = nextRecords)
            if (!persistLocked(next, backupPrevious = true)) {
                throw BatchStoreException("persist_failed",
                    "could not persist the manual confirmation; not recorded, retry")
            }
            content = next
            return mc
        }
    }

    /**
     * Restore from the backup (SPEC §2.5). Operator-explicit only:
     * never automatic at startup. Records in a terminal state with
     * a valid result are kept; every other record becomes UNKNOWN
     * (unknownAt = the restore time, except a record that was
     * already UNKNOWN, which keeps its unknownAt). The restore sets
     * recoveryPending=true, so /batch stays refused until the
     * operator completes verification through completeRecovery().
     *
     * The .bak is at most one persist behind the live file, so a
     * batch executed after the backup was written is absent from
     * the restored set — that gap is why the recovery gate exists.
     *
     * @throws BatchStoreException (503 batch_store_unavailable when
     * there is no usable backup; 503 persist_failed when the
     * restored store cannot be written)
     */
    fun restoreFromBackup(operatorNote: String): RestoreSummary {
        synchronized(lock) {
            ensureInitialized()
            // The .bak read distinguishes "no backup" (null) from
            // "backup unreadable" (throws): both stay fail-closed,
            // but the cause is reported honestly (CONS-152 P1-01).
            val bakText: String?
            try {
                bakText = readFileText(File(dir, BAK_FILE))
            } catch (e: Exception) {
                throw BatchStoreException("batch_store_unavailable",
                    "backup file is unreadable: " + e.message)
            }
            val bakContent: StoreContent
            try {
                if (bakText == null) {
                    throw BatchStoreException("batch_store_unavailable",
                        "no usable backup file; /batch stays unavailable")
                }
                checkStoreSize(bakText)
                bakContent = storeFromJson(JSONObject(bakText))
            } catch (e: BatchStoreException) {
                throw e
            } catch (e: Exception) {
                throw BatchStoreException("batch_store_unavailable",
                    "backup file is corrupt: " + e.message)
            }
            val now = System.currentTimeMillis()
            val restored = LinkedHashMap<String, BatchRecord>()
            var converted = 0
            for ((id, rec) in bakContent.records) {
                if (rec.status in TERMINAL_STATUSES && rec.result != null) {
                    restored[id] = rec
                } else {
                    val unknownAt =
                        if (rec.status == "UNKNOWN" && rec.unknownAt != null) rec.unknownAt else now
                    restored[id] = toUnknown(rec, unknownAt)
                    converted++
                }
            }
            val recoveryInfo = JSONObject()
                .put("restoredAt", now)
                .put("backupUsed", BAK_FILE)
                .put("note", operatorNote)
            val next = StoreContent(restored, true, recoveryInfo, content.durabilityVerified)
            // Do NOT touch the .bak here: it is the source we just
            // restored from.
            if (!persistLocked(next, backupPrevious = false)) {
                throw BatchStoreException("persist_failed",
                    "could not persist the restored store")
            }
            content = next
            storeState = StoreState.NORMAL
            BridgeLog.log("batch_registry",
                "restored " + restored.size + " record(s) from backup; "
                    + converted + " converted to UNKNOWN; recovery pending")
            return RestoreSummary(restored.size, converted)
        }
    }

    /**
     * Deliberately clear the store (SPEC §2.5). One atomic state
     * transition: the gated-empty content is written via temp +
     * flush + fsync + rename, and only after the rename succeeds is
     * the clear complete. A crash before the rename leaves the old
     * (corrupt) file and /batch stays fail-closed; a crash after it
     * leaves recoveryPending=true. No crash point produces "Normal
     * empty". The clear does NOT touch the .bak file.
     */
    fun clearStore(operatorNote: String) {
        synchronized(lock) {
            ensureInitialized()
            val now = System.currentTimeMillis()
            val recoveryInfo = JSONObject()
                .put("clearedAt", now)
                .put("reason", "store_clear")
                .put("note", operatorNote)
            val next = StoreContent(LinkedHashMap(), true, recoveryInfo, content.durabilityVerified)
            if (!persistLocked(next, backupPrevious = false)) {
                throw BatchStoreException("persist_failed",
                    "could not persist the cleared store; the old file remains")
            }
            content = next
            storeState = StoreState.NORMAL
            BridgeLog.log("batch_registry", "store cleared by operator; recovery pending")
        }
    }

    /**
     * Complete the recovery verification (SPEC §2.5). The operator
     * has checked the phone's execution history; /batch resumes.
     * The gate is persisted, so a restart does not clear it — only
     * this explicit operation does.
     *
     * @throws BatchBadRequestException (400) when no recovery is
     * pending
     */
    fun completeRecovery(completedBy: String) {
        synchronized(lock) {
            ensureInitialized()
            requireNotCorruptLocked()
            if (!content.recoveryPending) {
                throw BatchBadRequestException("recovery is not pending")
            }
            // Copy the info object: the stored one must not be
            // mutated before the persist succeeds.
            val info = JSONObject(content.recoveryInfo?.toString() ?: "{}")
            info.put("completedAt", System.currentTimeMillis())
            info.put("completedBy", completedBy)
            val next = content.copy(recoveryPending = false, recoveryInfo = info)
            if (!persistLocked(next, backupPrevious = true)) {
                throw BatchStoreException("persist_failed",
                    "could not persist the recovery completion")
            }
            content = next
            BridgeLog.log("batch_registry", "recovery verification completed by " + completedBy)
        }
    }

    /**
     * Record that the phone-side storage durability has been
     * demonstrated (SPEC §2.2, C-01-18). Called through the
     * management endpoint after the §5 real-device
     * power-loss-equivalent test passes. The marker records the
     * storage method it was verified for; if the method changes,
     * re-verification is required before /batch resumes.
     *
     * @throws BatchBadRequestException (400) for an unknown method
     */
    fun recordDurabilityVerified(method: String, note: String) {
        synchronized(lock) {
            ensureInitialized()
            // A corrupt store must never be overwritten with an
            // empty store + marker: that would silently erase the
            // corrupt record and bypass the recovery gate (CONS-152
            // P0-02). The recovery paths are restore / clear, not
            // this endpoint.
            requireNotCorruptLocked()
            if (method != STORAGE_METHOD) {
                throw BatchBadRequestException("unknown storage method: " + method)
            }
            val marker = JSONObject()
                .put("method", method)
                .put("verifiedAt", System.currentTimeMillis())
                .put("note", note)
            val next = content.copy(durabilityVerified = marker)
            if (!persistLocked(next, backupPrevious = true)) {
                throw BatchStoreException("persist_failed",
                    "could not persist the durability marker")
            }
            content = next
            BridgeLog.log("batch_registry",
                "durability verified for method " + method + "; /batch resumes")
        }
    }

    /** The durability gate (SPEC §2.2): the marker must exist and
     * name this build's storage method. Caller must hold the lock. */
    private fun isDurabilityVerifiedLocked(): Boolean {
        val marker = content.durabilityVerified ?: return false
        return marker.optString("method", "") == STORAGE_METHOD
    }

    /**
     * Persist a store content with the atomic write (SPEC §2.2):
     * write the full new content to a temp file in the same
     * directory, flush, fsync, rename over the live file, fsync
     * the directory entry (BridgeLog.atomicWriteText). Before the
     * live rename, the previous live content is copied to the .bak
     * (unless backupPrevious is false: the clear and the restore
     * must not touch the .bak). A failed .bak write is logged but
     * does not fail the main persist. Returns true only when the
     * live write succeeded; on success lastPersistedJson is updated.
     * Does NOT change `content` — the caller commits on success.
     */
    private fun persistLocked(next: StoreContent, backupPrevious: Boolean): Boolean {
        val json = storeToJson(next).toString()
        // Whole-store cap (SPEC §2.7, CONS-152 P2-01): the 512 KB
        // cap is enforced on EVERY persist path, not just admit().
        // A persist that would exceed it fails like any other
        // persist failure, and the caller treats it as such.
        if (json.toByteArray(StandardCharsets.UTF_8).size > MAX_FILE_BYTES) {
            BridgeLog.log("batch_registry",
                "persist refused: store would exceed 512 KB")
            return false
        }
        if (backupPrevious) {
            val prev = lastPersistedJson
            if (prev != null) {
                if (!BridgeLog.atomicWriteText(dir, BAK_FILE, prev)) {
                    BridgeLog.log("batch_registry",
                        "backup write failed; main persist proceeds")
                }
            }
        }
        if (!BridgeLog.atomicWriteText(dir, STORE_FILE, json)) {
            return false
        }
        lastPersistedJson = json
        return true
    }

    /** The serialized size of one record, in UTF-8 bytes. */
    private fun recordJsonBytes(record: BatchRecord): Int {
        return recordToJson(record).toString().toByteArray(StandardCharsets.UTF_8).size
    }

    // ---- JSON: store ----

    private fun storeToJson(c: StoreContent): JSONObject {
        val root = JSONObject()
        root.put("format_version", 1)
        val arr = JSONArray()
        for ((_, rec) in c.records) {
            arr.put(recordToJson(rec))
        }
        root.put("records", arr)
        root.put("recoveryPending", c.recoveryPending)
        if (c.recoveryInfo != null) root.put("recoveryInfo", c.recoveryInfo)
        else root.put("recoveryInfo", JSONObject.NULL)
        if (c.durabilityVerified != null) root.put("durabilityVerified", c.durabilityVerified)
        else root.put("durabilityVerified", JSONObject.NULL)
        return root
    }

    /**
     * The whole-store cap on load (SPEC §2.7, CONS-152 P2-01): a
     * file over 512 KB is corrupt, never silently accepted.
     * @throws IllegalArgumentException when the file is oversize
     */
    private fun checkStoreSize(text: String) {
        if (text.toByteArray(StandardCharsets.UTF_8).size > MAX_FILE_BYTES) {
            throw IllegalArgumentException("store file exceeds 512 KB")
        }
    }

    /**
     * Parse and strictly validate a store file (SPEC §2.2). Unknown
     * extra fields are ignored (forward compatibility), but a
     * missing required field, a wrong type, a duplicate batch_id,
     * or an UNKNOWN record without unknownAt is corruption.
     * @throws IllegalArgumentException on any violation
     */
    private fun storeFromJson(root: JSONObject): StoreContent {
        val formatVersion = requireInt(root, "format_version")
        if (formatVersion != 1) {
            throw IllegalArgumentException("unsupported format_version: " + formatVersion)
        }
        if (!root.has("records") || root.get("records") !is JSONArray) {
            throw IllegalArgumentException("records must be an array")
        }
        val records = LinkedHashMap<String, BatchRecord>()
        val arr = root.getJSONArray("records")
        // The 100-record cap is enforced on load too (CONS-152
        // P2-01): an over-cap file is corrupt, never silently
        // accepted.
        if (arr.length() > MAX_RECORDS) {
            throw IllegalArgumentException("too many records: " + arr.length())
        }
        for (i in 0 until arr.length()) {
            val obj = arr.get(i)
            if (obj !is JSONObject) {
                throw IllegalArgumentException("record " + i + " is not an object")
            }
            val rec = recordFromJson(obj)
            if (records.containsKey(rec.batchId)) {
                throw IllegalArgumentException("duplicate batch_id: " + rec.batchId)
            }
            records[rec.batchId] = rec
        }
        if (!root.has("recoveryPending") || root.get("recoveryPending") !is Boolean) {
            throw IllegalArgumentException("recoveryPending must be a boolean")
        }
        val recoveryPending = root.getBoolean("recoveryPending")
        val recoveryInfo = if (root.isNull("recoveryInfo")) null else root.optJSONObject("recoveryInfo")
        if (!root.isNull("recoveryInfo") && recoveryInfo == null) {
            throw IllegalArgumentException("recoveryInfo must be an object or null")
        }
        val durabilityVerified =
            if (root.isNull("durabilityVerified")) null else root.optJSONObject("durabilityVerified")
        if (!root.isNull("durabilityVerified") && durabilityVerified == null) {
            throw IllegalArgumentException("durabilityVerified must be an object or null")
        }
        if (durabilityVerified != null) {
            requireString(durabilityVerified, "method")
            requireLong(durabilityVerified, "verifiedAt")
        }
        return StoreContent(records, recoveryPending, recoveryInfo, durabilityVerified)
    }

    // ---- JSON: record ----

    private fun recordToJson(rec: BatchRecord): JSONObject {
        val obj = JSONObject()
        obj.put("batch_id", rec.batchId)
        obj.put("payload_hash", rec.payloadHash)
        obj.put("status", rec.status)
        if (rec.result != null) obj.put("result", batchResultToJson(rec.result))
        else obj.put("result", JSONObject.NULL)
        obj.put("createdAt", rec.createdAt)
        obj.put("expiresAt", rec.expiresAt)
        obj.put("tombstoneUntil", rec.tombstoneUntil)
        if (rec.unknownAt != null) obj.put("unknownAt", rec.unknownAt)
        else obj.put("unknownAt", JSONObject.NULL)
        if (rec.manualConfirmation != null) {
            obj.put("manualConfirmation", manualConfirmationToJson(rec.manualConfirmation))
        } else {
            obj.put("manualConfirmation", JSONObject.NULL)
        }
        if (rec.supersedes != null) obj.put("supersedes", rec.supersedes)
        else obj.put("supersedes", JSONObject.NULL)
        return obj
    }

    private fun recordFromJson(obj: JSONObject): BatchRecord {
        val batchId = requireString(obj, "batch_id")
        if (batchId.isEmpty() || batchId.length > MAX_BATCH_ID_LEN) {
            throw IllegalArgumentException("invalid batch_id")
        }
        val payloadHash = requireString(obj, "payload_hash")
        if (payloadHash.length != 64
            || !payloadHash.all { it in '0'..'9' || it in 'a'..'f' }
        ) {
            throw IllegalArgumentException("invalid payload_hash for " + batchId)
        }
        val status = requireString(obj, "status")
        if (status != "IN_PROGRESS" && status !in TERMINAL_STATUSES && status != "UNKNOWN") {
            throw IllegalArgumentException("invalid status for " + batchId + ": " + status)
        }
        val result: BridgeAccessibilityService.BatchResult?
        if (status in TERMINAL_STATUSES) {
            if (obj.isNull("result")) {
                throw IllegalArgumentException("terminal record without result: " + batchId)
            }
            val resultObj = obj.optJSONObject("result")
                ?: throw IllegalArgumentException("result must be an object: " + batchId)
            result = batchResultFromJson(resultObj, batchId)
        } else {
            if (!obj.isNull("result")) {
                throw IllegalArgumentException("non-terminal record with a result: " + batchId)
            }
            result = null
        }
        val createdAt = requireLong(obj, "createdAt")
        val expiresAt = requireLong(obj, "expiresAt")
        val tombstoneUntil = requireLong(obj, "tombstoneUntil")
        if (createdAt < 0 || expiresAt < createdAt || tombstoneUntil < expiresAt) {
            throw IllegalArgumentException("invalid retention times for " + batchId)
        }
        val unknownAt: Long?
        if (status == "UNKNOWN") {
            if (obj.isNull("unknownAt")) {
                throw IllegalArgumentException("UNKNOWN record without unknownAt: " + batchId)
            }
            unknownAt = requireLong(obj, "unknownAt")
            if (unknownAt < createdAt) {
                throw IllegalArgumentException("unknownAt before createdAt: " + batchId)
            }
        } else {
            if (!obj.isNull("unknownAt")) {
                throw IllegalArgumentException("non-UNKNOWN record with unknownAt: " + batchId)
            }
            unknownAt = null
        }
        val manualConfirmation: ManualConfirmation?
        if (obj.isNull("manualConfirmation")) {
            manualConfirmation = null
        } else {
            val mcObj = obj.optJSONObject("manualConfirmation")
                ?: throw IllegalArgumentException("manualConfirmation must be an object: " + batchId)
            manualConfirmation = manualConfirmationFromJson(mcObj, batchId)
        }
        val supersedes: String?
        if (obj.isNull("supersedes")) {
            supersedes = null
        } else {
            supersedes = requireString(obj, "supersedes")
            if (supersedes.isEmpty() || supersedes.length > MAX_BATCH_ID_LEN) {
                throw IllegalArgumentException("invalid supersedes for " + batchId)
            }
        }
        return BatchRecord(
            batchId, payloadHash, status, result, createdAt, expiresAt,
            tombstoneUntil, unknownAt, manualConfirmation, supersedes
        )
    }

    // ---- JSON: BatchResult (the full stored result, SPEC §2.2) ----

    private fun batchResultToJson(
        result: BridgeAccessibilityService.BatchResult
    ): JSONObject {
        val obj = JSONObject()
        obj.put("batch_id", result.batchId)
        obj.put("snapshot_id", result.snapshotId)
        obj.put("status", result.status)
        obj.put("payload_hash", result.payloadHash)
        obj.put("replayed", result.replayed)
        val results = JSONArray()
        for (r in result.results) {
            val rObj = JSONObject()
            rObj.put("action_id", r.actionId)
            rObj.put("status", r.status)
            if (r.error.isEmpty()) rObj.put("error", JSONObject.NULL) else rObj.put("error", r.error)
            rObj.put("hash_before", r.hashBefore)
            rObj.put("hash_after", r.hashAfter)
            results.put(rObj)
        }
        obj.put("results", results)
        val completed = JSONArray()
        for (id in result.completedActionIds) {
            completed.put(id)
        }
        obj.put("completed_action_ids", completed)
        if (result.failedActionId.isEmpty()) obj.put("failed_action_id", JSONObject.NULL)
        else obj.put("failed_action_id", result.failedActionId)
        obj.put("stopped_at", result.stoppedAt)
        if (result.stopReason.isEmpty()) obj.put("stop_reason", JSONObject.NULL)
        else obj.put("stop_reason", result.stopReason)
        return obj
    }

    private fun batchResultFromJson(
        obj: JSONObject,
        batchId: String
    ): BridgeAccessibilityService.BatchResult {
        val result = BridgeAccessibilityService.BatchResult()
        result.batchId = requireString(obj, "batch_id")
        if (result.batchId != batchId) {
            throw IllegalArgumentException("result batch_id mismatch: " + batchId)
        }
        result.snapshotId = requireInt(obj, "snapshot_id")
        result.status = requireString(obj, "status")
        // The stored result keeps the wire format (lowercase:
        // "completed" / "partial" / "failed"), identical to the
        // original HTTP response (§2.2) — so the terminal check
        // must normalize, like complete() does. Comparing the
        // lowercase wire status against the uppercase SPEC
        // vocabulary directly would reject every correctly
        // persisted result on the next load (live failure
        // 2026-11-13, C-01-4 follow-up).
        if (result.status.uppercase() !in TERMINAL_STATUSES) {
            throw IllegalArgumentException("stored result is not terminal: " + batchId)
        }
        result.payloadHash = requireString(obj, "payload_hash")
        // `replayed` is read but not trusted: the copy constructor
        // leaves it false, and the replay path sets it true.
        if (!obj.has("results") || obj.get("results") !is JSONArray) {
            throw IllegalArgumentException("results must be an array: " + batchId)
        }
        val arr = obj.getJSONArray("results")
        for (i in 0 until arr.length()) {
            val rObj = arr.get(i)
            if (rObj !is JSONObject) {
                throw IllegalArgumentException("action result is not an object: " + batchId)
            }
            val r = BridgeAccessibilityService.ActionResult()
            r.actionId = requireString(rObj, "action_id")
            r.status = requireString(rObj, "status")
            r.error = if (rObj.isNull("error")) "" else requireString(rObj, "error")
            r.hashBefore = requireInt(rObj, "hash_before")
            r.hashAfter = requireInt(rObj, "hash_after")
            result.results.add(r)
        }
        if (!obj.has("completed_action_ids") || obj.get("completed_action_ids") !is JSONArray) {
            throw IllegalArgumentException("completed_action_ids must be an array: " + batchId)
        }
        val completed = obj.getJSONArray("completed_action_ids")
        for (i in 0 until completed.length()) {
            result.completedActionIds.add(requireString(completed, i))
        }
        result.failedActionId =
            if (obj.isNull("failed_action_id")) "" else requireString(obj, "failed_action_id")
        result.stoppedAt = requireInt(obj, "stopped_at")
        result.stopReason =
            if (obj.isNull("stop_reason")) "" else requireString(obj, "stop_reason")
        return result
    }

    // ---- JSON: manual confirmation ----

    private fun manualConfirmationToJson(mc: ManualConfirmation): JSONObject {
        return JSONObject()
            .put("confirmedAt", mc.confirmedAt)
            .put("confirmedBy", mc.confirmedBy)
            .put("confirmedVia", mc.confirmedVia)
            .put("outcome", mc.outcome)
            .put("note", mc.note)
    }

    private fun manualConfirmationFromJson(obj: JSONObject, batchId: String): ManualConfirmation {
        val confirmedAt = requireLong(obj, "confirmedAt")
        val confirmedBy = requireString(obj, "confirmedBy")
        val confirmedVia = requireString(obj, "confirmedVia")
        val outcome = requireString(obj, "outcome")
        if (outcome != "executed" && outcome != "not_executed" && outcome != "partial") {
            throw IllegalArgumentException("invalid confirmation outcome: " + batchId)
        }
        val note = requireString(obj, "note")
        return ManualConfirmation(confirmedAt, confirmedBy, confirmedVia, outcome, note)
    }

    // ---- JSON validation helpers (strict: a missing field or a
    // wrong type is corruption, SPEC §2.2) ----

    private fun requireString(obj: JSONObject, key: String): String {
        if (!obj.has(key) || obj.isNull(key)) {
            throw IllegalArgumentException("missing field: " + key)
        }
        val value = obj.get(key)
        if (value !is String) {
            throw IllegalArgumentException("field is not a string: " + key)
        }
        return value
    }

    private fun requireString(arr: JSONArray, index: Int): String {
        val value = arr.get(index)
        if (value !is String) {
            throw IllegalArgumentException("array element is not a string at " + index)
        }
        return value
    }

    private fun requireInt(obj: JSONObject, key: String): Int {
        if (!obj.has(key) || obj.isNull(key)) {
            throw IllegalArgumentException("missing field: " + key)
        }
        val value = obj.get(key)
        if (value !is Number) {
            throw IllegalArgumentException("field is not a number: " + key)
        }
        return value.toInt()
    }

    private fun requireLong(obj: JSONObject, key: String): Long {
        if (!obj.has(key) || obj.isNull(key)) {
            throw IllegalArgumentException("missing field: " + key)
        }
        val value = obj.get(key)
        if (value !is Number) {
            throw IllegalArgumentException("field is not a number: " + key)
        }
        return value.toLong()
    }
}
