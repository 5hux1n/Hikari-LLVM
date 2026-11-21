#pragma once

// Frontend attributes keep selected functions intact before the plugin runs.
#define HIKARI_PROTECT \
  __attribute__((noinline, optnone, \
                 annotate("hikari_preserve bcf fla indibr sub strenc constenc")))
#define HIKARI_GUARD __attribute__((annotate("antihook ah_antirebind")))
