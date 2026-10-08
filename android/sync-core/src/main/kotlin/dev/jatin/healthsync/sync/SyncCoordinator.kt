package dev.jatin.healthsync.sync

import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * The only entry point for running or resetting sync. One process-wide mutex
 * serialises periodic sync, Sync now, Full Resync, origin changes and restore
 * resets, so sync state is never reset while a run is using it.
 * The app must hold exactly one instance.
 */
class SyncCoordinator(
    private val engine: SyncEngine,
    private val store: StateStore,
) {
    private val mutex = Mutex()

    val isRunning: Boolean get() = mutex.isLocked

    suspend fun sync(): SyncEngine.Outcome = mutex.withLock { engine.run() }

    /** Reset happens inside the lock, as the first step of the resync run. */
    suspend fun fullResync(): SyncEngine.Outcome = mutex.withLock {
        store.update { it.resetForFullResync() }
        engine.run()
    }

    /** Saving a different origin policy invalidates all sync progress (forces a Full Resync). */
    suspend fun setOrigins(origins: Map<String, Set<String>>) = mutex.withLock {
        store.update { s ->
            if (s.originsConfirmed && s.allowedOrigins == origins) s
            else s.resetForFullResync().copy(allowedOrigins = origins, originsConfirmed = true)
        }
    }

    /** Restored/migrated install: drop everything (tokens included) and start over. */
    suspend fun resetAll() = mutex.withLock { store.update { SyncState() } }
}

/** For tests and as a reference implementation of the StateStore contract. */
class InMemoryStateStore(initial: SyncState = SyncState()) : StateStore {
    private val lock = Mutex()
    var state: SyncState = initial
        private set

    override suspend fun read(): SyncState = lock.withLock { state }
    override suspend fun update(transform: (SyncState) -> SyncState): SyncState = lock.withLock {
        state = transform(state)
        state
    }
}
