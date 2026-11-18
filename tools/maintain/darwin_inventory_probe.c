/* Capability investigation only; never linked into the production runtime.
 * Use the runner SDK's kinfo_proc declarations, not copied ABI offsets.
 * Apple sources: xnu/bsd/sys/{sysctl,proc}.h and adv_cmds/ps/ps.c.
 */
#define _DARWIN_C_SOURCE 1
#include <sys/types.h>
#include <sys/sysctl.h>
#include <sys/proc.h>
#include <errno.h>
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define MAX_BYTES (16u * 1024u * 1024u)
#define MAX_ATTEMPTS 4
#define MAX_SELECTED 8
#define DEADLINE_NS UINT64_C(5000000000)
#if defined(__arm64__) || defined(__aarch64__)
#define NATIVE_ARCH "arm64"
#elif defined(__x86_64__)
#define NATIVE_ARCH "x86_64"
#else
#define NATIVE_ARCH "unsupported"
#endif
#define SIGNED_FIELD(value) _Generic((value), \
    char: (CHAR_MIN < 0), signed char: 1, unsigned char: 0, \
    short: 1, unsigned short: 0, int: 1, unsigned int: 0, \
    long: 1, unsigned long: 0, long long: 1, unsigned long long: 0)
#define FIELD(name, member, comma) \
    printf("%s\"%s\":{\"offset\":%zu,\"width\":%zu,\"signed\":%s}", \
        comma, name, offsetof(struct kinfo_proc, member), \
        sizeof(((struct kinfo_proc *)0)->member), \
        SIGNED_FIELD(((struct kinfo_proc *)0)->member) ? "true" : "false")

struct attempt {
    size_t capacity, returned;
    int error;
    uint64_t elapsed_ns;
};

static uint64_t now_ns(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
}

static void layout(void) {
    unsigned int one = 1;
    printf("{\"architecture\":\"%s\",\"record_size\":%zu,\"alignment\":%zu,\"pointer_width\":%zu,"
           "\"endianness\":\"%s\",\"fields\":{",
           NATIVE_ARCH, sizeof(struct kinfo_proc), _Alignof(struct kinfo_proc), sizeof(void *),
           *(unsigned char *)&one ? "little" : "big");
    FIELD("pid", kp_proc.p_pid, "");
    FIELD("parent", kp_eproc.e_ppid, ",");
    FIELD("status", kp_proc.p_stat, ",");
    FIELD("start_seconds", kp_proc.p_starttime.tv_sec, ",");
    FIELD("start_micros", kp_proc.p_starttime.tv_usec, ",");
    printf("}}");
}

static int emit(const char *site, int error, struct kinfo_proc *table, size_t count,
                struct attempt *attempts, size_t used, pid_t *selected, size_t wanted) {
    printf("{\"kind\":\"exoma-darwin-inventory-native\",\"schema_version\":1,"
           "\"ok\":%s,\"transport_complete\":%s,\"total_records\":%zu,"
           "\"limits\":{\"max_bytes\":%u,\"max_attempts\":%d,\"deadline_ns\":%llu},"
           "\"layout\":", error ? "false" : "true",
           !error && table ? "true" : "false", error ? 0 : count,
           MAX_BYTES, MAX_ATTEMPTS, (unsigned long long)DEADLINE_NS);
    layout();
    printf(",\"error\":");
    if (error) printf("{\"site\":\"%s\",\"errno\":%d}", site, error);
    else printf("null");
    printf(",\"attempts\":[");
    for (size_t i = 0; i < used; ++i) {
        printf("%s{\"capacity\":%zu,\"returned_bytes\":%zu,\"errno\":%d,\"elapsed_ns\":%llu}",
               i ? "," : "", attempts[i].capacity, attempts[i].returned,
               attempts[i].error, (unsigned long long)attempts[i].elapsed_ns);
    }
    printf("],\"records\":[");
    int comma = 0;
    if (!error) {
        for (size_t i = 0; i < count; ++i) {
            for (size_t j = 0; j < wanted; ++j) {
                if (table[i].kp_proc.p_pid != selected[j]) continue;
                printf("%s{\"pid\":%lld,\"parent\":%lld,\"status\":%d,"
                       "\"start_seconds\":%lld,\"start_micros\":%lld}",
                       comma++ ? "," : "", (long long)table[i].kp_proc.p_pid,
                       (long long)table[i].kp_eproc.e_ppid, (int)table[i].kp_proc.p_stat,
                       (long long)table[i].kp_proc.p_starttime.tv_sec,
                       (long long)table[i].kp_proc.p_starttime.tv_usec);
            }
        }
    }
    printf("]}\n");
    return error ? 2 : 0;
}

static int compare_pid(const void *left, const void *right) {
    pid_t a = *(const pid_t *)left, b = *(const pid_t *)right;
    return (a > b) - (a < b);
}

int main(int argc, char **argv) {
    struct attempt attempts[MAX_ATTEMPTS] = {{0}};
    pid_t selected[MAX_SELECTED] = {0};
    size_t wanted = 0, used = 0, count = 0, next_capacity = 0;
    struct kinfo_proc *table = NULL;
    int error = EINVAL, mib[4] = {CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0};
    const char *site = "arguments";
    if (argc == 2 && strcmp(argv[1], "layout") == 0) {
        printf("{\"kind\":\"exoma-darwin-inventory-layout\",\"schema_version\":1,\"layout\":");
        layout();
        printf("}\n");
        return 0;
    }
    if (argc < 3 || argc > 2 + MAX_SELECTED || strcmp(argv[1], "snapshot") != 0)
        return emit(site, error, NULL, 0, attempts, 0, selected, 0);
    for (int i = 2; i < argc; ++i) {
        char *end = NULL;
        errno = 0;
        long value = strtol(argv[i], &end, 10);
        if (errno || !end || *end || value <= 0 || value > INT_MAX)
            return emit(site, EINVAL, NULL, 0, attempts, 0, selected, 0);
        for (size_t j = 0; j < wanted; ++j)
            if (selected[j] == (pid_t)value)
                return emit(site, EINVAL, NULL, 0, attempts, 0, selected, 0);
        selected[wanted++] = (pid_t)value;
    }
    uint64_t started = now_ns();
    if (!started) return emit("clock", EIO, NULL, 0, attempts, 0, selected, wanted);
    for (size_t i = 0; i < MAX_ATTEMPTS; ++i) {
        size_t needed = 0;
        uint64_t before = now_ns();
        used = i + 1;
        if (!before || before - started >= DEADLINE_NS) {
            site = "deadline"; error = ETIMEDOUT; break;
        }
        if (sysctl(mib, 4, NULL, &needed, NULL, 0) != 0) {
            site = "size_query"; error = errno ? errno : EIO; attempts[i].error = error; break;
        }
        if (needed < sizeof(*table) || needed > MAX_BYTES) {
            site = "size_bounds"; error = EOVERFLOW; break;
        }
        size_t slack = needed / 4;
        if (slack < sizeof(*table) * 16) slack = sizeof(*table) * 16;
        size_t capacity = slack > MAX_BYTES - needed ? MAX_BYTES : needed + slack;
        if (capacity < next_capacity) capacity = next_capacity;
        attempts[i].capacity = capacity;
        table = malloc(capacity);
        if (!table) { site = "allocation"; error = ENOMEM; break; }
        size_t returned = capacity;
        errno = 0;
        int result = sysctl(mib, 4, table, &returned, NULL, 0);
        error = result ? (errno ? errno : EIO) : 0;
        uint64_t after = now_ns();
        attempts[i].returned = returned;
        attempts[i].error = error;
        attempts[i].elapsed_ns = after >= before ? after - before : 0;
        if (!after || after - started >= DEADLINE_NS) {
            site = "deadline"; error = ETIMEDOUT; break;
        }
        if (error) {
            free(table); table = NULL;
            if (error != ENOMEM) { site = "fill"; break; }
            site = "growth_exhausted";
            if (capacity == MAX_BYTES) break;
            next_capacity = capacity > MAX_BYTES / 2 ? MAX_BYTES : capacity * 2;
            continue;  /* Failed/partial fills are never decoded. */
        }
        if (returned > capacity || returned % sizeof(*table) != 0 || returned == 0) {
            site = "record_length"; error = EPROTO; break;
        }
        count = returned / sizeof(*table);
        pid_t *pids = malloc(count * sizeof(*pids));
        if (!pids) { site = "allocation"; error = ENOMEM; break; }
        for (size_t j = 0; j < count; ++j) pids[j] = table[j].kp_proc.p_pid;
        qsort(pids, count, sizeof(*pids), compare_pid);
        for (size_t j = 0; j < count; ++j) {
            if (pids[j] < 0 || (j && pids[j] == pids[j - 1])) {
                site = "duplicate_or_invalid_pid"; error = EPROTO; break;
            }
        }
        free(pids);
        break;
    }
    int code = emit(site, error, table, count, attempts, used, selected, wanted);
    free(table);
    return code;
}
