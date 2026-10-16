/* PURPOSE: Record exec-boundary environment names before the Python fixture runtime starts.
 * CONTRACT: Independent RT-MCP-001 AC-MCP-07 probe; no production or credential reads.
 * DEPENDS: POSIX C runtime, test-owned CODEX_HOME, and compile-time protected fake CLI path.
 * CEILING: Captures names only; it is not a production launcher or sandbox. */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef FAKE_CODEX_PATH
#error FAKE_CODEX_PATH must name the protected Python process double
#endif

extern char **environ;

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "exec") == 0) {
        const char *home = getenv("CODEX_HOME");
        if (home == NULL) return 125;
        /* Suffix storage fits its fixed filename and decimal PID; no environment value is logged. */
        size_t capacity = strlen(home) + sizeof("/fixture-boundary-env.") + 3 * sizeof(pid_t) + 32;
        char *path = malloc(capacity);
        if (path == NULL) return 125;
        snprintf(path, capacity, "%s/fixture-boundary-env.%ld.bin", home, (long)getpid());
        int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
        free(path);
        if (fd < 0) return 125;
        FILE *stream = fdopen(fd, "wb");
        if (stream == NULL) { close(fd); return 125; }
        for (char **entry = environ; *entry != NULL; ++entry) {
            const char *equals = strchr(*entry, '=');
            if (equals == NULL) { fclose(stream); return 125; }
            size_t size = (size_t)(equals - *entry);
            if (fwrite(*entry, 1, size, stream) != size || fputc(0, stream) == EOF) {
                fclose(stream);
                return 125;
            }
        }
        if (fclose(stream) != 0) return 125;
    }
    argv[0] = FAKE_CODEX_PATH;
    execv(FAKE_CODEX_PATH, argv);
    return 126;
}
