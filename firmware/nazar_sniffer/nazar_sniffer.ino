// nazar_sniffer.ino — NAZAR ESP32 promiscuous capture  (F1-F4)
//
// Board: ESP32 Dev Module (ESP32 WROOM-32, CH340C).  Upload Speed 460800.
// Sketch folder name must match this filename.
//
// PRD §7.  Producer/consumer split:
//   callback (core 0, ~2000x/sec) -> accumulate bytes into a fixed table  [fast, silent]
//   loop()   (core 1, 10x/sec)    -> snapshot, zero, print CSV            [slow, chatty]
// The callback never prints, never allocates, never blocks.  Printing inside it
// would stall the Wi-Fi driver and drop the very packets the correlation needs.
//
// SYNC WARNING: BIN_MS, MAX_DEV and the baud rate below are duplicated in
// backend/config.py.  There is no build system to enforce this.  Change both in
// the same commit.

#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "nvs_flash.h"

#define SERIAL_BAUD    460800   // must match config.SERIAL_BAUD (921600 corrupts on this CH340C)
#define BIN_MS         100      // must match config.BIN_MS
#define MAX_DEV        40       // must match config.MAX_DEVICES
#define SCAN_DWELL_MS  600      // must match config.SCAN_DWELL_MS
#define CHAN_MIN       1
#define CHAN_MAX       13
#define IDLE_BINS      50       // evict a silent device after 5 s to keep slots free
#define STATUS_EVERY   10       // emit #DROP once per second

// ── Shared state ─────────────────────────────────────────────────────
typedef struct {
  uint8_t  mac[6];
  uint32_t up_bytes, dn_bytes, up_pkts;
  int8_t   rssi;
  uint16_t idle;
  bool     used;
} nz_dev_t;   // not "dev_t": that name is a POSIX typedef in the toolchain headers

static nz_dev_t table[MAX_DEV];
static portMUX_TYPE tableMux = portMUX_INITIALIZER_UNLOCKED;

// Drop counters.  Silence is the enemy: a gap in the bitrate series produces a
// corrupted correlation that still looks perfectly healthy.  These make it loud.
static volatile uint32_t drop_table_full = 0;   // no free slot for a new MAC
static volatile uint32_t drop_unparsed   = 0;   // runt / ad-hoc / WDS frame
static volatile uint32_t fc_tods = 0, fc_fromds = 0, fc_other = 0;  // direction census
static uint32_t drop_serial   = 0;              // TX buffer too full to emit a line
static uint32_t drop_late_bin = 0;              // loop() missed a 100 ms deadline

static uint8_t  cur_channel = 6;
static bool     locked      = false;
static uint32_t next_bin_ms = 0;
static uint8_t  status_tick = 0;

// ── Producer: promiscuous callback, core 0 ───────────────────────────
static void IRAM_ATTR snifferCb(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_DATA) return;
  const wifi_promiscuous_pkt_t *p = (wifi_promiscuous_pkt_t *)buf;
  const uint8_t *f = p->payload;
  const uint16_t len = p->rx_ctrl.sig_len;
  if (len < 24) { drop_unparsed++; return; }        // shorter than an 802.11 header

  // Direction bits in FrameCtl byte 1 decide which address is the device in the room.
  const bool toDS   = f[1] & 0x01;
  const bool fromDS = f[1] & 0x02;
  const uint8_t *mac;
  bool uplink;
  if (toDS && !fromDS)      { mac = f + 10; uplink = true;  fc_tods++;   }   // station -> AP
  else if (!toDS && fromDS) { mac = f + 4;  uplink = false; fc_fromds++; }   // AP -> station
  else                      { fc_other++; drop_unparsed++; return; }         // ad-hoc / WDS

  // A group/broadcast address is never a single device in the room. Counting
  // ff:ff:ff:ff:ff:ff would burn a table slot on the AP's broadcast traffic.
  if (mac[0] & 0x01) { drop_unparsed++; return; }

  portENTER_CRITICAL(&tableMux);
  int slot = -1;
  for (int i = 0; i < MAX_DEV; i++) {
    if (table[i].used && memcmp(table[i].mac, mac, 6) == 0) { slot = i; break; }
    if (!table[i].used && slot < 0) slot = i;                   // remember first free
  }
  if (slot < 0) { drop_table_full++; portEXIT_CRITICAL(&tableMux); return; }
  nz_dev_t *d = &table[slot];
  if (!d->used) { memcpy(d->mac, mac, 6); d->used = true;
                  d->up_bytes = d->dn_bytes = d->up_pkts = 0; }
  if (uplink) { d->up_bytes += len; d->up_pkts++; }             // uplink is the signal
  else        { d->dn_bytes += len; }
  d->rssi = p->rx_ctrl.rssi;
  d->idle = 0;
  portEXIT_CRITICAL(&tableMux);
}

// ── Radio ────────────────────────────────────────────────────────────
static void radioStart() {
  nvs_flash_init();
  // Without a default event loop the Wi-Fi driver cannot post events and spams
  // "failed to post WiFi event=43 ret=259" onto the data UART.
  esp_event_loop_create_default();
  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  esp_wifi_init(&cfg);
  esp_wifi_set_storage(WIFI_STORAGE_RAM);
  esp_wifi_set_mode(WIFI_MODE_NULL);
  esp_wifi_start();
  wifi_promiscuous_filter_t filt = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
  esp_wifi_set_promiscuous_filter(&filt);
  esp_wifi_set_promiscuous_rx_cb(&snifferCb);
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(cur_channel, WIFI_SECOND_CHAN_NONE);
}

static void setChannel(uint8_t ch) {
  cur_channel = ch;
  esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
}

static void tableClear() {
  portENTER_CRITICAL(&tableMux);
  memset(table, 0, sizeof(table));
  portEXIT_CRITICAL(&tableMux);
}

// ── Consumer: snapshot under lock, print outside it ──────────────────
// Holds the lock only long enough to memcpy and zero, then releases before
// printing.  A lost += between cores is a wrong byte count -> wrong bitrate ->
// failed detection, with no error message anywhere.
static int snapshot(nz_dev_t *out) {
  portENTER_CRITICAL(&tableMux);
  memcpy(out, table, sizeof(table));
  for (int i = 0; i < MAX_DEV; i++) {
    if (!table[i].used) continue;
    bool active = table[i].up_bytes || table[i].dn_bytes;
    table[i].up_bytes = table[i].dn_bytes = table[i].up_pkts = 0;
    table[i].idle = active ? 0 : table[i].idle + 1;
    if (table[i].idle > IDLE_BINS) table[i].used = false;        // free the slot
  }
  portEXIT_CRITICAL(&tableMux);
  return MAX_DEV;
}

// Emit one line only if the TX buffer can take it without blocking.  Blocking
// loop() would silently skip whole bins, which is worse than a counted drop.
static void emit(const char *line) {
  int n = strlen(line);
  if (Serial.availableForWrite() < n + 1) { drop_serial++; return; }
  Serial.println(line);
}

static void macStr(const uint8_t *m, char *out) {
  snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
}

static void flushBin(uint32_t t_ms) {
  static nz_dev_t snap[MAX_DEV];
  snapshot(snap);
  char mac[18], line[80];
  for (int i = 0; i < MAX_DEV; i++) {
    if (!snap[i].used) continue;
    if (!snap[i].up_bytes && !snap[i].dn_bytes) continue;
    macStr(snap[i].mac, mac);
    snprintf(line, sizeof(line), "D,%lu,%s,%lu,%lu,%lu,%d",
             (unsigned long)t_ms, mac,
             (unsigned long)snap[i].up_bytes, (unsigned long)snap[i].dn_bytes,
             (unsigned long)snap[i].up_pkts, snap[i].rssi);
    emit(line);
  }
  if (++status_tick >= STATUS_EVERY) {
    status_tick = 0;
    snprintf(line, sizeof(line), "#DROP,%lu,%lu,%lu,%lu,%lu",
             (unsigned long)t_ms, (unsigned long)drop_table_full,
             (unsigned long)drop_serial, (unsigned long)drop_late_bin,
             (unsigned long)drop_unparsed);
    Serial.println(line);   // status must never be dropped
  }
}

// ── F3: channel scan ─────────────────────────────────────────────────
static void channelScan() {
  static nz_dev_t snap[MAX_DEV];
  char mac[18], line[80];
  locked = false;
  for (uint8_t ch = CHAN_MIN; ch <= CHAN_MAX; ch++) {
    setChannel(ch);
    tableClear();
    delay(SCAN_DWELL_MS);
    snapshot(snap);
    for (int i = 0; i < MAX_DEV; i++) {
      if (!snap[i].used) continue;
      if (!snap[i].up_bytes && !snap[i].dn_bytes) continue;
      macStr(snap[i].mac, mac);
      snprintf(line, sizeof(line), "S,%u,%s,%lu,%lu,%lu,%d",
               ch, mac, (unsigned long)snap[i].up_bytes,
               (unsigned long)snap[i].dn_bytes, (unsigned long)snap[i].up_pkts,
               snap[i].rssi);
      Serial.println(line);
    }
  }
  Serial.println("#SCAN_PASS_DONE");
}

static void printState() {
  char line[160];
  snprintf(line, sizeof(line), "#STATE,%s,%u,%d,%d,%lu,%lu,%lu,%lu,tods=%lu,fromds=%lu,other=%lu",
           locked ? "LOCKED" : "IDLE", cur_channel, BIN_MS, MAX_DEV,
           (unsigned long)drop_table_full, (unsigned long)drop_serial,
           (unsigned long)drop_late_bin, (unsigned long)drop_unparsed,
           (unsigned long)fc_tods, (unsigned long)fc_fromds, (unsigned long)fc_other);
  Serial.println(line);
}

// ── F4: channel lock ─────────────────────────────────────────────────
static void handleCommand() {
  int c = Serial.read();
  if (c < 0) return;
  if (c == 'S') { channelScan(); return; }
  if (c == '?') { printState(); return; }
  if (c == 'C') {
    // Accept C6 or C13, terminated by newline or a short read timeout.
    char buf[4] = {0}; int n = 0;
    uint32_t t0 = millis();
    while (n < 3 && millis() - t0 < 50) {
      int d = Serial.peek();
      if (d < 0) continue;
      if (d < '0' || d > '9') { Serial.read(); break; }
      buf[n++] = Serial.read();
    }
    int ch = atoi(buf);
    if (ch < CHAN_MIN || ch > CHAN_MAX) { Serial.println("#ERR,channel must be 1-13"); return; }
    setChannel((uint8_t)ch);
    tableClear();
    locked = true;
    next_bin_ms = millis() + BIN_MS;
    status_tick = 0;
    // Echoed so the host can prove the lock took.  A board still hopping while
    // the host believes it is locked produces a silently meaningless capture.
    Serial.printf("#LOCK,%d\n", ch);
  }
}

void setup() {
  // Default TX buffer is the 128-byte hardware FIFO — one bin carrying a
  // handful of devices overflows it and emit() starts discarding lines.
  // Worst case is 40 devices x ~48 bytes = ~1.9 KB per bin.
  Serial.setTxBufferSize(4096);
  Serial.begin(SERIAL_BAUD);
  // UART0 is a data channel, not a console. Any library log line lands in the
  // middle of a CSV row and corrupts it — a truncated RSSI, a bad byte count.
  // Nothing may write here except this sketch.
  esp_log_level_set("*", ESP_LOG_NONE);
  delay(200);
  memset(table, 0, sizeof(table));
  radioStart();
  Serial.println("#NAZAR_SNIFFER_READY");
}

void loop() {
  handleCommand();
  if (!locked) { delay(2); return; }

  uint32_t now = millis();
  if ((int32_t)(now - next_bin_ms) < 0) { delay(1); return; }

  // If we are more than a whole bin late, bins were missed.  Count them and
  // resynchronise rather than emitting a burst of catch-up lines with wrong
  // timestamps — a stretched time axis would shift the correlation lag.
  while ((int32_t)(now - next_bin_ms) >= BIN_MS) { next_bin_ms += BIN_MS; drop_late_bin++; }

  uint32_t bin_t = next_bin_ms;
  next_bin_ms += BIN_MS;
  flushBin(bin_t);
}
