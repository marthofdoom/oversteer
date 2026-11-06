# New game checklist

What Richard Burns Rally taught us. Goal: given the same physics inputs every game feels the same, and any negative
deviation gets corrected. Check each of these from a recorded drive when adding a game, against ACR's numbers, and
run `scripts/ffb-parity.py` before and after every FFB change. Compare absolute levels at one mapping, not only ratios
to the game's own grip, and refresh the reference drive whenever the chain changes.

## Steering input
- Speed sensitivity or "speed fade": wheel position against the game's reported steering, in bands of speed. The ratio
  must be flat. RBR's Speed fade took it from 100 % stopped to 70 % at 35 m/s.
- Steering curve or linearity: the same table across wheel angle must be flat.
- Wheel range against the car's lock: the ratio at low speed tells whether the range is right.

## Force
- Derived, not played: the force comes from physics telemetry, never from the game's FFB signal.
- Level: force against lateral load, against the reference, at the same mapping. RBR sat at about half of ACR's.
- Anything fitted at runtime: does what it applies match what it has evidence for? RBR's strength fit ran 20-32 % light.
- Filters: measure what each one removes, by band, against the reference.
- The game's own strength setting must not change what is played: find it from the data, as RBR's 25 % showed.
- Force in a slide against force in grip (body slip 0.2-0.4 rad against |ay| 5-9 m/s²). ACR holds 0.81. RBR fell to 0.35
  until the caster floor. Check for a missing caster term first.
- Surface overload: road detail swamping the force. RBR needed a 15 Hz cut on the column torque.
- Engine revs, kerbs and road texture: can each be felt, at what share of the force?
- Scale: absolute torque in Nm through the same chain, not a share of the game's output.
- Take-over within seconds on generic priors, never waiting silently.
- Total added delay against the 50 ms soft limit.

## Controls and car setup
- Clutch: does the pedal change the rpm-to-wheel ratio? RBR's setups carry ClutchHelp.
- Assists hidden in car setups or plugin options, and files the launcher overwrites.
- Car identity: telemetry fingerprint against the game's own car list.

## Co-driver (rally games)
- Language slots and which ones the game offers; which files each slot and each plugin setting really play.
- Call timing and grouping against ACR: DA_PacenoteConfig's Normal looks 5.75 s ahead.
