import {Config} from "@remotion/cli/config";

// Parity with the local renderer image: Remotion defaults concurrency to
// cpus/2 (=1 on the 2-core VM); studio/local renders should use both cores.
Config.setConcurrency(2);
