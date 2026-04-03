// =============================================================================
// Scilab ( https://www.scilab.org/ ) - This file is part of Scilab
// Copyright (C) 2023 - Dassault Systèmes S.E. - Clément DAVID
//
//  This file is distributed under the same license as the Scilab package.
// =============================================================================
//

// <-- CLI SHELL MODE -->
// <-- NO CHECK REF -->

loadXcosLibs();

props = scicos_params(tf=100);

objs = list()
objs(1) = FROMWSB("define");
objs(2) = GAINBLK_f("define");
objs(3) = TRASH_f("define");
objs(4) = CLOCK_c("define");
objs(5) = scicos_link(from=[1 1 0], to=[2 1 1]);
objs(6) = scicos_link(from=[2 1 0], to=[3 1 1]);
objs(7) = scicos_link(from=[4 1 0], to=[3 1 1], ct=[1 -1]);

scs_m = scicos_diagram(props=props, objs=objs);

V = struct("time", (1:100)', "values", sin((0:0.1:9.9)'));
scicos_simulate(scs_m, list());

// switch the GAINBLK_f parameters
scs_m.objs(2).graphics.exprs = "[1 1 1]";

V.values = [V.values V.values V.values];
scicos_simulate(scs_m, list());
