# Що стокові девайси показують повз `parameters`

> Згенеровано `node tools/device-audit.mjs` на живому Live.
> Девайсів опитано: 85, не завантажилось: 0.

Параметри покриті `DeviceParamSet`-ом усі -- він не знає про конкретний
девайс. Тут перелічене те, чого серед `parameters` немає: саме такий стан
ми досі втрачали, і саме через нього маркери семплу в Simpler не їхали.

## Девайси з обʼєктним станом (17)

Відсіяно як шум: списки можливих значень (*_list -- це вміст випадайки,
а не стан), маршрутизація девайса (audio_inputs, midi_outputs -- залізо
в кожного своє) і macros_mapped (похідне від параметрів).

| Девайс | Категорія | Параметрів | Обʼєкти й колекції | Власні скаляри |
|---|---|---:|---|---|
| Audio Effect Rack | audio_effects | 18 | chain_selector:DeviceParameter, chains[0], return_chains[0] | selected_variation_index, variation_count |
| CC Control | midi_effects | 17 | — | custom_bool_target, custom_float_target_0, custom_float_target_1, custom_float_target_10, custom_float_target_11, custom_float_target_2, custom_float_target_3, custom_float_target_4, custom_float_target_5, custom_float_target_6, custom_float_target_7, custom_float_target_8, custom_float_target_9 |
| Compressor | audio_effects | 23 | input_routing_channel:RoutingChannel, input_routing_type:RoutingType, available_input_routing_channels[1], available_input_routing_types[7] | — |
| Drift | instruments | 66 | — | mod_matrix_filter_source_1_index, mod_matrix_filter_source_2_index, mod_matrix_lfo_source_index, mod_matrix_pitch_source_1_index, mod_matrix_pitch_source_2_index, mod_matrix_shape_source_index, mod_matrix_source_1_index, mod_matrix_source_2_index, mod_matrix_source_3_index, mod_matrix_target_1_index, mod_matrix_target_2_index, mod_matrix_target_3_index, pitch_bend_range, voice_count_index, voice_mode_index |
| Drum Rack | instruments | 17 | chains[0], drum_pads[128], return_chains[0], visible_drum_pads[16] | selected_variation_index, variation_count |
| Drum Sampler | instruments | 40 | — | gain |
| EQ Eight | audio_effects | 84 | — | edit_mode, global_mode, oversample |
| Hybrid Reverb | audio_effects | 54 | — | ir_attack_time, ir_category_index, ir_decay_time, ir_file_index, ir_size_factor, ir_time_shaping_on |
| Instrument Rack | instruments | 18 | chain_selector:DeviceParameter, chains[0], return_chains[0] | selected_variation_index, variation_count |
| Looper | audio_effects | 9 | — | loop_length, overdub_after_record, record_length_index, tempo |
| Meld | instruments | 129 | — | mono_poly, poly_voices, selected_engine, unison_voices |
| MIDI Effect Rack | midi_effects | 18 | chain_selector:DeviceParameter, chains[0], return_chains[0] | selected_variation_index, variation_count |
| Roar | audio_effects | 91 | — | env_listen, routing_mode_index |
| Shifter | audio_effects | 36 | — | pitch_bend_range, pitch_mode_index |
| Simpler | instruments | 63 | — | multi_sample_mode, note_pitch_bend_range, pad_slicing, pitch_bend_range, playback_mode, retrigger, sample, slicing_playback_mode, voices |
| Spectral Resonator | audio_effects | 20 | — | frequency_dial_mode, midi_gate, mod_mode, mono_poly, pitch_bend_range, pitch_mode, polyphony |
| Wavetable | instruments | 93 | oscillator_1_wavetables[29], oscillator_2_wavetables[29], oscillator_wavetable_categories[12], visible_modulation_target_names[4] | filter_routing, mono_poly, oscillator_1_effect_mode, oscillator_1_wavetable_category, oscillator_1_wavetable_index, oscillator_2_effect_mode, oscillator_2_wavetable_category, oscillator_2_wavetable_index, poly_voices, unison_mode, unison_voice_count |

### Спільні скаляри, які має кожен девайс

can_compare_ab, can_have_chains, can_have_drum_pads, class_display_name, class_name, is_active, latency_in_ms, latency_in_samples, name, type

**Межа цієї проби.** Девайс опитується щойно завантаженим, тобто порожнім.
Стан, який зʼявляється лише разом із вмістом, вона не побачить: у Simpler
без семплу немає обʼєкта sample -- того самого, через який маркери на хвилі
й не синхронізувались. Порожній рядок тут означає «нічого понад параметри
у ПОРОЖНЬОМУ девайсі», а не «нічого взагалі».

## Решта (68) -- лише параметри

Align Delay, Amp, Analog, Arpeggiator, Auto Filter, Auto Pan-Tremolo, Auto Shift, Beat Repeat, Cabinet, Channel EQ, Chord, Chorus-Ensemble, Collision, Corpus, DS Clang, DS Clap, DS Cymbal, DS FM, DS HH, DS Kick, DS Snare, DS Tom, Delay, Drum Buss, Dynamic Tube, EQ Three, Echo, Electric, Envelope Follower, Envelope MIDI, Erosion, Expression Control, External Audio Effect, External Instrument, Filter Delay, Gate, Glue Compressor, Grain Delay, Impulse, LFO, Limiter, MIDI Monitor, MPE Control, Multiband Dynamics, Note Echo, Note Length, Operator, Overdrive, Pedal, Phaser-Flanger, Pitch, Random, Redux, Resonators, Reverb, Sampler, Saturator, Scale, Shaper, Shaper MIDI, Spectral Time, Spectrum, Tension, Tuner, Utility, Velocity, Vinyl Distortion, Vocoder
