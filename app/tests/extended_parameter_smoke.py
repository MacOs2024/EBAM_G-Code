from __future__ import annotations
import json, traceback, re, math
from dataclasses import replace
from pathlib import Path
from ebam_gcode_studio.core import ProcessSettings, generate_rotational_shell

PARAMS={"profile_type":"straight_cup","height_mm":6.0,"bottom_diameter_mm":79.0,"top_diameter_mm":79.0,"max_diameter_mm":79.0,"wall_thickness_mm":1.0,"bottom_solid_mm":0.0,"resolution":96}
BASE=ProcessSettings(layer_height=0.5,hatch_spacing=1.0,rotational_path_strategy="rotary_c_rings",rotational_radial_step_mm=1.0,rotary_c_motion_mode="no_pause_flat_rings",rotary_c_transition_angle_deg=17,rotary_c_max_deg_min=10000,feed_bottom_mm_min=450,feed_top_mm_min=450,beam_current_mode="current",beam_current_bottom_ma=22.5,beam_current_top_ma=22.5,wire_feed_mode="auto",rotary_c_disable_layer_pauses=True,rotary_c_disable_w_retract=True,rotary_c_disable_z_hop=True)
VALUES={
"edge_offset":[0.0,0.8,2.0],"min_segment_length":[0.1,4.0,10.0],"center_xy":[False,True,False],"z_to_zero":[True,False,True],
"axis_order":["XYZ","XZY","YXZ"],"rotate_x_deg":[0,15,-15],"rotate_y_deg":[0,15,-15],"rotate_z_deg":[0,45,90],"mirror_x":[False,True,False],"mirror_y":[False,True,False],"mirror_z":[False,True,False],
"output_offset_x_mm":[0,10,-5],"output_offset_y_mm":[0,10,-5],"output_offset_z_mm":[0,5,10],"direction":["Y-","Y+","X-"],"alternate_hatch_shift":[True,False,True],"shift_fraction_a":[0,0.2,-0.2],"shift_fraction_b":[0,0.33,0.5],"shift_fraction_c":[0,-0.33,-0.5],"thermal_ordering":["natural","skip_neighbours","natural"],
"deposition_strategy":["continuous","segmented","continuous"],"link_feed_factor":[1.0,1.3,2.0],"rotational_path_strategy":["rotary_c_rings","rings","spiral"],"rotary_c_relative_turns":[True,False,True],"rotary_c_motion_mode":["no_pause_flat_rings","separate_rings","no_pause_flat_rings"],"rotary_c_continuous_keep_beam_wire_on":[False,True,False],"rotary_c_disable_layer_pauses":[False,True,False],"rotary_c_disable_w_retract":[False,True,False],"rotary_c_disable_z_hop":[False,True,False],
"alternate_layer_rotation":[False,True,False],"contour_passes":[0,1,2],"contour_offset_step":[0.3,0.7,1.2],"contour_wire_factor":[0.5,0.72,1.0],"contour_feed_factor":[0.5,0.88,1.2],"contour_every_n_layers":[1,2,3],"contour_first":[False,True,False],
"adaptive_thin_wall":[True,False,True],"force_contour_on_empty_layers":[True,False,True],"adaptive_section_probe":[True,False,True],"section_probe_fraction":[0.1,0.45,0.9],"thin_wall_hatch_spacing_factor":[0.3,0.55,0.8],"thin_wall_edge_offset_factor":[0.1,0.35,0.7],"thin_wall_min_segment_length":[0.1,1.0,2.0],"thin_wall_wire_factor":[0.3,0.65,0.9],"adaptive_wire_correction":[True,False,True],"manual_section_fallback":[True,False,True],"projection_fallback_if_empty":[False,True,False],"minimum_generated_layer_fraction":[0.5,0.9,1.0],"progress_update_every_layers":[1,2,5],
"target_total_time_s":[0,120,360],"target_time_mode":["off","feed_only","feed_layer_hatch"],
"lead_in_beam_mm":[0,0.6,2.0],"soft_start_mm":[0,2,5],"soft_finish_mm":[0,1.8,5],"tail_beam_mm":[0,0.8,2],"soft_wire_factor":[0.5,0.82,1.0],"edge_wire_factor_bottom":[0.5,0.94,1.0],"edge_wire_factor_top":[0.5,0.87,1.0],"near_edge_wire_factor_bottom":[0.5,0.97,1.0],"near_edge_wire_factor_top":[0.5,0.93,1.0],
"units":["mm","мм","mm"],"bormash_x_min_mm":[0,10,100],"bormash_x_max_mm":[100,3670,5000],"bormash_y_min_mm":[0,10,100],"bormash_y_max_mm":[100,1510,3000],"bormash_z_min_mm":[0,1,5],"bormash_z_max_mm":[20,1443,2000],"max_gcode_lines_warning":[100,250000,1000000],"max_gcode_size_mb_warning":[1,8,100],
}

def quick_invariants(res):
 st=res.stats; g=res.gcode
 assert st.get('gcode_lines', len(g.splitlines())) > 0
 assert 'M68 E0 Q0.000' in g and 'M68 E2 Q0.000' in g
 if str(st.get('rotary_c_motion_mode','')).lower()=='no_pause_flat_rings':
   lines=g.splitlines(); start=next(i for i,l in enumerate(lines) if l.startswith('G91')); end=next(i for i,l in enumerate(lines[start+1:], start+1) if l.startswith('G90'))
   block=lines[start+1:end]
   assert not any(l.startswith('M68') or l.startswith('G4') or l.startswith('W') for l in block)
   assert sum(1 for l in block if re.search(r'G1\s+C-?360\.000',l)) == int(st['layers_total'])
 return True

results=[]; failures=[]
for field, vals in VALUES.items():
 for v in vals:
  try:
   s=replace(BASE, **{field:v})
   # keep invalid combinations valid
   if field=='rotational_path_strategy' and v != 'rotary_c_rings':
     s=replace(s, rotary_c_motion_mode='separate_rings')
   res=generate_rotational_shell(PARAMS, s)
   quick_invariants(res)
   results.append({'name':f'{field}={v}','ok':True,'mode':res.stats.get('rotary_c_motion_mode',res.stats.get('rotational_path_strategy')),'lines':res.stats.get('gcode_lines',len(res.gcode.splitlines()))})
  except Exception as e:
   failures.append({'name':f'{field}={v}','error':str(e),'traceback':traceback.format_exc(limit=5)})
   results.append({'name':f'{field}={v}','ok':False,'error':str(e)})
summary={'cases_total':len(results),'cases_ok':sum(1 for r in results if r['ok']),'cases_failed':sum(1 for r in results if not r['ok']),'failures':failures[:20]}
Path('extended_parameter_smoke_results.json').write_text(json.dumps({'summary':summary,'results':results,'failures':failures},indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps(summary,indent=2,ensure_ascii=False))
