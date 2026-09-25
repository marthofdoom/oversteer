/* oversteer-shm-bridge: forward a game's Windows shared-memory telemetry
 * to Oversteer's rev lights over UDP.
 *
 * Games built on the Assetto Corsa family (Assetto Corsa, Assetto Corsa
 * Competizione, Assetto Corsa Rally) publish telemetry only through named
 * shared memory (Local\acpmf_physics, Local\acpmf_static). Under Proton
 * that lives inside the game's Wine prefix, so this small Windows program
 * runs there alongside the game (see oversteer-run) and sends what the rev
 * lights need to Oversteer on the host as a 24-byte "OVST" datagram.
 *
 * All three games share the same prefix of the physics and static structs
 * (packetId, gas, brake, fuel, gear, rpms / ... sectorCount, maxTorque,
 * maxPower, maxRpm), which is all that is read here.
 *
 * Built for the Windows (GUI) subsystem so no console window appears when it
 * runs next to the game; all output goes to the --log file.
 * Build (any mingw): x86_64-w64-mingw32-gcc -O2 -s -mwindows -o oversteer-shm-bridge.exe oversteer-shm-bridge.c -lws2_32
 *   or: zig cc -target x86_64-windows-gnu -O2 -s -Wl,--subsystem,windows -o oversteer-shm-bridge.exe oversteer-shm-bridge.c -lws2_32
 *
 * Usage: oversteer-shm-bridge.exe [--host 127.0.0.1] [--port 5300] [--rate 60] [--verbose] [--exit-when-gone] [--log FILE]
 *   --log FILE: also append messages to FILE (Proton doesn't pass a console
 *   program's stderr through, so this is how oversteer-run keeps a log).
 *   --exit-when-gone: once telemetry has been seen, exit when it is gone
 *   for a few seconds (the game has quit) instead of waiting for the next one.
 *
 * This file is part of Oversteer (GPL-3.0-or-later).
 */
#include <winsock2.h>
#include <windows.h>
#include <tlhelp32.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdarg.h>


/* Offsets into the shared structs (4-byte packing, identical in AC/ACC/ACR) */
#define PHYS_PACKET_ID   0     /* int */
#define PHYS_GAS         4     /* float 0..1 */
#define PHYS_BRAKE       8     /* float 0..1 */
#define PHYS_GEAR        16    /* int, 0 = reverse, 1 = neutral, 2 = first ... */
#define PHYS_RPMS        20    /* int */
#define PHYS_SPEED_KMH   28    /* float */
#define PHYS_STEER       24    /* float */
#define PHYS_ACCG        44    /* float[3] */
#define PHYS_WHEEL_SLIP  56    /* float[4] */
#define PHYS_WHEEL_LOAD  72    /* float[4] */
#define PHYS_WHEEL_ANG   104   /* float[4], rad/s */
#define PHYS_SUSP_TRAVEL 184   /* float[4], m */
#define PHYS_AUTO_SHIFT  264   /* int */
#define PHYS_RIDE_HEIGHT 268   /* float[2] */
#define PHYS_LOCAL_ANG   296   /* float[3] */
#define PHYS_CLUTCH      364   /* float */
#define PHYS_BRAKE_BIAS  564   /* float */
#define PHYS_LOCAL_VEL   568   /* float[3] */
#define PHYS_MAX_RPM_NOW 588   /* int, ACC/ACR */
#define PHYS_FX          608   /* float[4], ACC/ACR */
#define PHYS_FY          624   /* float[4], ACC/ACR */
/* The physics page is 800 bytes in ACC/ACR and 580 in AC; map the most
 * the section allows (a view larger than the section fails). */
static const size_t phys_sizes[] = { 800, 580, 64, 0 };
#define STATIC_MAX_RPM   412   /* int; after smVersion/acVersion (2x15 wchar), 2 ints,
                                  5x33 wchar strings, sectorCount, maxTorque, maxPower */
#define STATIC_CAR_MODEL 68    /* wchar_t[33] */
#define STATIC_TRACK     134   /* wchar_t[33] */
#define STATIC_SUSP_MAX  420   /* float[4] */
#define STATIC_TYRE_R    436   /* float[4] */
#define STATIC_SPLINE_LEN 520  /* float, the track's (stage's) length in m */
static const size_t static_sizes[] = { 820, 684, 524, 416, 0 };

/* Graphics page. The first 252 bytes are shared; after that AC and
 * ACC/ACR differ (ACC keeps every car's coordinates). These offsets are
 * from the published structs and wait on an ACR capture (--verbose
 * logs them once a second) before Oversteer relies on them. */
#define GRAPH_SESSION    8     /* int */
#define GRAPH_DISTANCE   156   /* float, m */
#define GRAPH_IN_PIT     160   /* int */
#define GRAPH_LAPS       172   /* int, numberOfLaps */
#define GRAPH_SPLINE_POS 248   /* float 0..1 */
#define GRAPH_AC_COORDS  252   /* float[3], AC */
#define GRAPH_AC_GRIP    280   /* float, AC */
#define GRAPH_ACC_ACTIVE 252   /* int, ACC/ACR */
#define GRAPH_ACC_COORDS 256   /* float[60][3], ACC/ACR */
#define GRAPH_ACC_IDS    976   /* int[60] */
#define GRAPH_ACC_PLAYER 1216  /* int */
#define GRAPH_ACC_GRIP   1240  /* float */
static const size_t graph_sizes[] = { 1588, 1316, 300, 0 };

#define OVST_VERSION 3
#define GAME_AC  1
#define GAME_ACC 2
#define GAME_ACR 3
#define OVST_SOURCE_ACPMF 1

#pragma pack(push, 1)
struct ovst_packet {
    char magic[4];       /* "OVST" */
    uint8_t version;
    uint8_t source;
    uint16_t flags;      /* bit 0: shift light */
    float rpm;
    float max_rpm;       /* 0 when unknown */
    int32_t gear;        /* -1 reverse, 0 neutral, 1.. */
    float speed_kmh;
    /* version 2 */
    float gas;           /* 0..1 */
    float brake;
    char car[32];        /* static carModel, ASCII, NUL-padded */
    char track[32];      /* static track */
    /* version 3: NaN where the game's pages don't reach */
    uint8_t game;        /* GAME_AC, GAME_ACC, GAME_ACR, 0 unknown */
    uint8_t flags2;      /* bit 0 autoShifterOn, bit 1 isInPit */
    uint16_t reserved;
    float clutch, steer;
    float accg[3];
    float local_vel[3];
    float local_ang_vel[3];
    float wheel_slip[4];
    float wheel_ang_speed[4];
    float susp_travel[4];
    float wheel_load[4];
    float ride_height[2];
    float tyre_radius[4];
    float susp_max_travel[4];
    float fx[4], fy[4];
    float current_max_rpm;
    float track_length;
    float spline_pos, distance;
    float surface_grip, brake_bias;
    int32_t laps, session_type;
    float world_pos[3];
};
#pragma pack(pop)

static int verbose = 0;
static int exit_when_gone = 0;
static FILE *logfile = NULL;

static void logmsg(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "oversteer-shm-bridge: ");
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    va_end(ap);
    if (logfile) {
        va_start(ap, fmt);
        fprintf(logfile, "[%lu] ", (unsigned long)GetTickCount() / 1000);
        vfprintf(logfile, fmt, ap);
        fprintf(logfile, "\n");
        fflush(logfile);
        va_end(ap);
    }
}

/* The games create the mappings in the session-local namespace; try the
 * spellings that resolve to it, and the global one, so an unusual
 * launcher setup still works. */
static const char *physics_names[] = { "Local\\acpmf_physics", "acpmf_physics", "Global\\acpmf_physics", NULL };
static const char *graphics_names[] = { "Local\\acpmf_graphics", "acpmf_graphics", "Global\\acpmf_graphics", NULL };
static const char *static_names[] = { "Local\\acpmf_static", "acpmf_static", "Global\\acpmf_static", NULL };

static const void *open_any(const char **names, HANDLE *handle, size_t size, const char **used);

/* Is a process with this image name (case-insensitive) running? */
static int process_running(const char *image)
{
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    PROCESSENTRY32 pe;
    int found = 0;

    if (snap == INVALID_HANDLE_VALUE)
        return 1;   /* can't tell: assume yes */
    pe.dwSize = sizeof(pe);
    if (Process32First(snap, &pe)) {
        do {
            if (!_stricmp(pe.szExeFile, image)) {
                found = 1;
                break;
            }
        } while (Process32Next(snap, &pe));
    }
    CloseHandle(snap);
    return found;
}

static const void *open_view(const char *name, HANDLE *handle, size_t size)
{
    const void *view;

    *handle = OpenFileMappingA(FILE_MAP_READ, FALSE, name);
    if (*handle == NULL)
        return NULL;
    view = MapViewOfFile(*handle, FILE_MAP_READ, 0, 0, size);
    if (view == NULL) {
        CloseHandle(*handle);
        *handle = NULL;
    }
    return view;
}

static const void *open_any(const char **names, HANDLE *handle, size_t size, const char **used)
{
    const void *view = NULL;
    int i;

    for (i = 0; names[i] && view == NULL; i++) {
        view = open_view(names[i], handle, size);
        if (view && used)
            *used = names[i];
    }
    return view;
}

/* The largest of `sizes` that maps; *size is set to it (0 when none). */
static const void *open_sized(const char **names, HANDLE *handle, const size_t *sizes, size_t *size,
                              const char **used)
{
    const void *view = NULL;
    int i;

    for (i = 0; sizes[i] && view == NULL; i++) {
        view = open_any(names, handle, sizes[i], used);
        if (view)
            *size = sizes[i];
    }
    if (view == NULL)
        *size = 0;
    return view;
}

static void close_view(const void *view, HANDLE *handle)
{
    if (view)
        UnmapViewOfFile(view);
    if (*handle) {
        CloseHandle(*handle);
        *handle = NULL;
    }
}

static int32_t rd_i32(const void *base, size_t off)
{
    int32_t v;
    memcpy(&v, (const char *)base + off, sizeof(v));
    return v;
}

/* A wchar_t[33] from the static page as printable ASCII (others -> '_'). */
static void rd_name(const void *base, size_t off, char *out, size_t size)
{
    const uint16_t *w = (const uint16_t *)((const char *)base + off);
    size_t i;
    memset(out, 0, size);
    for (i = 0; i + 1 < size && i < 33 && w[i]; i++)
        out[i] = (w[i] >= 0x20 && w[i] < 0x7f) ? (char)w[i] : '_';
}

static float nan_f(void)
{
    union { uint32_t u; float f; } v = { 0x7fc00000u };
    return v.f;
}

static float rd_f32(const void *base, size_t off);

/* Floats from a page, NaN past the part that is mapped. */
static void rd_floats(const void *base, size_t size, size_t off, float *out, int n)
{
    int i;
    for (i = 0; i < n; i++)
        out[i] = (base && off + 4 * (i + 1) <= size) ? rd_f32(base, off + 4 * i) : nan_f();
}

static float rd_f32(const void *base, size_t off)
{
    float v;
    memcpy(&v, (const char *)base + off, sizeof(v));
    return v;
}

/* The version 3 fields; NaN (or 0) where a page is missing or shorter. */
static void fill_v3(struct ovst_packet *pkt, uint8_t game, const void *phys, size_t phys_size,
                    const void *stat, size_t stat_size, const void *graph, size_t graph_size)
{
    int i, player = -1;

    pkt->game = game;
    if (phys_size >= PHYS_AUTO_SHIFT + 4 && rd_i32(phys, PHYS_AUTO_SHIFT))
        pkt->flags2 |= 1;
    if (graph && graph_size >= GRAPH_IN_PIT + 4 && rd_i32(graph, GRAPH_IN_PIT))
        pkt->flags2 |= 2;
    rd_floats(phys, phys_size, PHYS_CLUTCH, &pkt->clutch, 1);
    rd_floats(phys, phys_size, PHYS_STEER, &pkt->steer, 1);
    rd_floats(phys, phys_size, PHYS_ACCG, pkt->accg, 3);
    rd_floats(phys, phys_size, PHYS_LOCAL_VEL, pkt->local_vel, 3);
    rd_floats(phys, phys_size, PHYS_LOCAL_ANG, pkt->local_ang_vel, 3);
    rd_floats(phys, phys_size, PHYS_WHEEL_SLIP, pkt->wheel_slip, 4);
    rd_floats(phys, phys_size, PHYS_WHEEL_ANG, pkt->wheel_ang_speed, 4);
    rd_floats(phys, phys_size, PHYS_SUSP_TRAVEL, pkt->susp_travel, 4);
    rd_floats(phys, phys_size, PHYS_WHEEL_LOAD, pkt->wheel_load, 4);
    rd_floats(phys, phys_size, PHYS_RIDE_HEIGHT, pkt->ride_height, 2);
    rd_floats(phys, phys_size, PHYS_BRAKE_BIAS, &pkt->brake_bias, 1);
    if (game != GAME_AC) {
        rd_floats(phys, phys_size, PHYS_FX, pkt->fx, 4);
        rd_floats(phys, phys_size, PHYS_FY, pkt->fy, 4);
        pkt->current_max_rpm = phys_size >= PHYS_MAX_RPM_NOW + 4 ? (float)rd_i32(phys, PHYS_MAX_RPM_NOW) : nan_f();
    } else {
        rd_floats(NULL, 0, 0, pkt->fx, 4);
        rd_floats(NULL, 0, 0, pkt->fy, 4);
        pkt->current_max_rpm = nan_f();
    }
    rd_floats(stat, stat_size, STATIC_TYRE_R, pkt->tyre_radius, 4);
    rd_floats(stat, stat_size, STATIC_SUSP_MAX, pkt->susp_max_travel, 4);
    rd_floats(stat, stat_size, STATIC_SPLINE_LEN, &pkt->track_length, 1);
    rd_floats(graph, graph_size, GRAPH_SPLINE_POS, &pkt->spline_pos, 1);
    rd_floats(graph, graph_size, GRAPH_DISTANCE, &pkt->distance, 1);
    pkt->laps = graph && graph_size >= GRAPH_LAPS + 4 ? rd_i32(graph, GRAPH_LAPS) : -1;
    pkt->session_type = graph && graph_size >= GRAPH_SESSION + 4 ? rd_i32(graph, GRAPH_SESSION) : -1;
    if (game == GAME_AC) {
        rd_floats(graph, graph_size, GRAPH_AC_COORDS, pkt->world_pos, 3);
        rd_floats(graph, graph_size, GRAPH_AC_GRIP, &pkt->surface_grip, 1);
    } else {
        /* ACC/ACR list every car; the player's entry is the one whose id
         * matches playerCarID */
        if (graph && graph_size >= GRAPH_ACC_PLAYER + 4) {
            int active = rd_i32(graph, GRAPH_ACC_ACTIVE), id = rd_i32(graph, GRAPH_ACC_PLAYER);
            for (i = 0; i < 60 && i < active; i++) {
                if (rd_i32(graph, GRAPH_ACC_IDS + 4 * i) == id) {
                    player = i;
                    break;
                }
            }
        }
        if (player >= 0)
            rd_floats(graph, graph_size, GRAPH_ACC_COORDS + 12 * player, pkt->world_pos, 3);
        else
            rd_floats(NULL, 0, 0, pkt->world_pos, 3);
        rd_floats(graph, graph_size, GRAPH_ACC_GRIP, &pkt->surface_grip, 1);
    }
}

int main(int argc, char **argv)
{
    const char *host = "127.0.0.1";
    const char *watch = NULL;
    int port = 5300, rate = 60;
    WSADATA wsa;
    SOCKET sock;
    struct sockaddr_in dest;
    HANDLE hphys = NULL, hstat = NULL, hgraph = NULL;
    const void *phys = NULL, *stat = NULL, *graph = NULL;
    size_t phys_size = 0, stat_size = 0, graph_size = 0;
    DWORD last_detail = 0;
    uint8_t game = 0;
    int32_t last_packet = -1;
    DWORD last_change = 0, last_report = 0, last_watch = 0;
    unsigned long sent = 0;
    const char *name_used = "?";
    int seen = 0, announced = 0, i;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--host") && i + 1 < argc)
            host = argv[++i];
        else if (!strcmp(argv[i], "--port") && i + 1 < argc)
            port = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--rate") && i + 1 < argc)
            rate = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--verbose"))
            verbose = 1;
        else if (!strcmp(argv[i], "--exit-when-gone"))
            exit_when_gone = 1;
        else if (!strcmp(argv[i], "--watch") && i + 1 < argc)
            watch = argv[++i];
        else if (!strcmp(argv[i], "--log") && i + 1 < argc)
            logfile = fopen(argv[++i], "a");
        else {
            fprintf(stderr, "usage: %s [--host H] [--port N] [--rate HZ] [--exit-when-gone] [--watch GAME.exe] [--log FILE] [--verbose]\n", argv[0]);
            return 2;
        }
    }
    if (port <= 0 || port > 65535 || rate <= 0 || rate > 1000) {
        logmsg("bad port or rate");
        return 2;
    }

    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        logmsg("WSAStartup failed");
        return 1;
    }
    sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock == INVALID_SOCKET) {
        logmsg("socket failed: %d", WSAGetLastError());
        return 1;
    }
    memset(&dest, 0, sizeof(dest));
    dest.sin_family = AF_INET;
    dest.sin_port = htons((unsigned short)port);
    dest.sin_addr.s_addr = inet_addr(host);
    if (dest.sin_addr.s_addr == INADDR_NONE) {
        logmsg("bad host %s", host);
        return 2;
    }
    /* Which game, from the executable oversteer-run watches */
    if (watch) {
        if (!_stricmp(watch, "acr.exe"))
            game = GAME_ACR;
        else if (!_stricmp(watch, "AC2-Win64-Shipping.exe"))
            game = GAME_ACC;
        else if (!_stricmp(watch, "acs.exe") || !_stricmp(watch, "acs_x86.exe"))
            game = GAME_AC;
    }
    logmsg("sending to %s:%d at %d Hz; waiting for %s%s%s", host, port, rate, physics_names[0],
           watch ? ", watching " : "", watch ? watch : "");

    for (;;) {
        struct ovst_packet pkt;
        int32_t packet_id;
        DWORD now = GetTickCount();

        if (watch && now - last_watch > 3000) {
            last_watch = now;
            if (!process_running(watch)) {
                logmsg("%s is not running, exiting", watch);
                return 0;
            }
        }

        if (phys == NULL) {
            phys = open_sized(physics_names, &hphys, phys_sizes, &phys_size, &name_used);
            if (phys == NULL) {
                if (seen && exit_when_gone) {
                    logmsg("telemetry gone, exiting");
                    return 0;
                }
                announced = 0;
                Sleep(1000);
                continue;
            }
            stat = open_sized(static_names, &hstat, static_sizes, &stat_size, NULL);
            graph = open_sized(graphics_names, &hgraph, graph_sizes, &graph_size, NULL);
            if (!game)
                game = phys_size >= 800 ? GAME_ACC : GAME_AC;   /* the layout, when the name didn't say */
            /* Don't re-send the packet that was there before: a paused
             * game keeps the same id until it resumes. */
            last_packet = rd_i32(phys, PHYS_PACKET_ID);
            last_change = now;
            seen = 1;
            if (!announced) {
                announced = 1;
                logmsg("telemetry found as %s (game %d; pages: physics %u, static %u, graphics %u; maxRpm %d, "
                       "stage length %.1f, packetId %d, rpms %d)", name_used, game, (unsigned)phys_size,
                       (unsigned)stat_size, (unsigned)graph_size, stat ? rd_i32(stat, STATIC_MAX_RPM) : 0,
                       stat_size >= 524 ? rd_f32(stat, STATIC_SPLINE_LEN) : -1.0f,
                       last_packet, rd_i32(phys, PHYS_RPMS));
            }
        }

        packet_id = rd_i32(phys, PHYS_PACKET_ID);
        if (packet_id != last_packet) {
            last_packet = packet_id;
            last_change = now;
            memset(&pkt, 0, sizeof(pkt));
            memcpy(pkt.magic, "OVST", 4);
            pkt.version = OVST_VERSION;
            pkt.source = OVST_SOURCE_ACPMF;
            pkt.rpm = (float)rd_i32(phys, PHYS_RPMS);
            pkt.max_rpm = stat ? (float)rd_i32(stat, STATIC_MAX_RPM) : 0.0f;
            pkt.gear = rd_i32(phys, PHYS_GEAR) - 1;
            pkt.speed_kmh = rd_f32(phys, PHYS_SPEED_KMH);
            pkt.gas = rd_f32(phys, PHYS_GAS);
            pkt.brake = rd_f32(phys, PHYS_BRAKE);
            if (stat) {
                rd_name(stat, STATIC_CAR_MODEL, pkt.car, sizeof(pkt.car));
                rd_name(stat, STATIC_TRACK, pkt.track, sizeof(pkt.track));
            }
            fill_v3(&pkt, game, phys, phys_size, stat, stat_size, graph, graph_size);
            if (verbose && now - last_detail >= 1000) {
                /* For confirming the offsets and signs on a real capture */
                last_detail = now;
                logmsg("v3 len %.1f pos %.4f dist %.1f grip %.3f laps %d session %d xyz %.1f %.1f %.1f | "
                       "steer %.3f clutch %.2f accg %.2f %.2f %.2f vel %.2f %.2f %.2f angvel %.2f %.2f %.2f | "
                       "susp %.3f %.3f %.3f %.3f of %.3f | ride %.3f %.3f | bias %.3f",
                       pkt.track_length, pkt.spline_pos, pkt.distance, pkt.surface_grip, pkt.laps,
                       pkt.session_type, pkt.world_pos[0], pkt.world_pos[1], pkt.world_pos[2],
                       pkt.steer, pkt.clutch, pkt.accg[0], pkt.accg[1], pkt.accg[2],
                       pkt.local_vel[0], pkt.local_vel[1], pkt.local_vel[2],
                       pkt.local_ang_vel[0], pkt.local_ang_vel[1], pkt.local_ang_vel[2],
                       pkt.susp_travel[0], pkt.susp_travel[1], pkt.susp_travel[2], pkt.susp_travel[3],
                       pkt.susp_max_travel[0], pkt.ride_height[0], pkt.ride_height[1], pkt.brake_bias);
            }
            if (sendto(sock, (const char *)&pkt, sizeof(pkt), 0, (struct sockaddr *)&dest, sizeof(dest)) == SOCKET_ERROR) {
                if (verbose)
                    logmsg("sendto failed: %d", WSAGetLastError());
            } else {
                sent++;
                if (verbose)
                    logmsg("rpm %.0f / %.0f gear %d", pkt.rpm, pkt.max_rpm, pkt.gear);
            }
        } else if (now - last_change > 2000) {
            /* Nothing written for two seconds: paused, in a menu, or quit.
             * Drop the views so we don't keep a quit game's section (and
             * its wineserver) alive; they are re-opened every second while
             * the mapping exists. */
            if (verbose)
                logmsg("telemetry stalled, releasing the mapping");
            close_view(phys, &hphys);
            close_view(stat, &hstat);
            close_view(graph, &hgraph);
            phys = stat = graph = NULL;
            Sleep(1000);
            continue;
        }
        if (now - last_report > 10000) {
            last_report = now;
            logmsg("packetId %d, rpm %d, %lu datagrams sent", packet_id, rd_i32(phys, PHYS_RPMS), sent);
        }
        Sleep(1000 / rate);
    }
}
