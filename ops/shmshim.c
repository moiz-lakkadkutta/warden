/* shmshim: System V shared-memory calls emulated with anonymous shared mmap.
 *
 * Token Factory Sandboxes run a kernel without CONFIG_SYSVIPC, so shmget() is ENOSYS. Postgres
 * (>= 9.3) still creates one tiny SysV segment as a data-directory interlock; the real shared
 * buffers already use mmap. LD_PRELOAD this into the postmaster only (see with-services).
 * Children inherit the mapping via fork(), so a per-process table is enough. A stale id from an
 * old postmaster.pid is unknown here -> EINVAL -> Postgres treats it as gone, which is correct
 * because every sandbox run is a fresh process tree.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/mman.h>
#include <sys/shm.h>
#include <sys/types.h>

#define MAXSEG 64

static struct {
    int used;
    key_t key;
    size_t size;
    void *addr;
    int nattch;
    int removed;
} seg[MAXSEG];

static int valid(int id) { return id >= 0 && id < MAXSEG && seg[id].used; }

int shmget(key_t key, size_t size, int flags) {
    if (key != IPC_PRIVATE) {
        for (int i = 0; i < MAXSEG; i++) {
            if (seg[i].used && !seg[i].removed && seg[i].key == key) {
                if ((flags & IPC_CREAT) && (flags & IPC_EXCL)) { errno = EEXIST; return -1; }
                if (size > seg[i].size) { errno = EINVAL; return -1; }
                return i;
            }
        }
        if (!(flags & IPC_CREAT)) { errno = ENOENT; return -1; }
    }
    for (int i = 0; i < MAXSEG; i++) {
        if (!seg[i].used) {
            void *p = mmap(NULL, size ? size : 1, PROT_READ | PROT_WRITE,
                           MAP_SHARED | MAP_ANONYMOUS, -1, 0);
            if (p == MAP_FAILED) return -1; /* errno from mmap */
            seg[i].used = 1; seg[i].key = key; seg[i].size = size;
            seg[i].addr = p; seg[i].nattch = 0; seg[i].removed = 0;
            return i;
        }
    }
    errno = ENOSPC;
    return -1;
}

void *shmat(int id, const void *addr, int flags) {
    (void)addr; (void)flags;
    if (!valid(id)) { errno = EINVAL; return (void *)-1; }
    seg[id].nattch++;
    return seg[id].addr;
}

int shmdt(const void *addr) {
    for (int i = 0; i < MAXSEG; i++) {
        if (seg[i].used && seg[i].addr == addr) {
            if (seg[i].nattch > 0) seg[i].nattch--;
            return 0;
        }
    }
    errno = EINVAL;
    return -1;
}

int shmctl(int id, int cmd, struct shmid_ds *buf) {
    if (!valid(id)) { errno = EINVAL; return -1; }
    switch (cmd) {
    case IPC_RMID:
        seg[id].removed = 1; /* mapping stays valid for current attachers, like the kernel */
        return 0;
    case IPC_STAT:
        if (buf) {
            memset(buf, 0, sizeof *buf);
            buf->shm_segsz = seg[id].size;
            buf->shm_nattch = (shmatt_t)seg[id].nattch;
            buf->shm_perm.__key = seg[id].key;
            buf->shm_perm.mode = 0600;
        }
        return 0;
    case IPC_SET:
        return 0;
    default:
        errno = EINVAL;
        return -1;
    }
}
