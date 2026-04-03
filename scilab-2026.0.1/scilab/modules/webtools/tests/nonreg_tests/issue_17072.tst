// =============================================================================
// Scilab ( https://www.scilab.org/ ) - This file is part of Scilab
// Copyright (C) 2023 - Dassault Systèmes S.E. - Cédric Delamarre
//
//  This file is distributed under the same license as the Scilab package.
// =============================================================================

// <-- CLI SHELL MODE -->
// <-- NO CHECK REF -->

// <-- Non-regression test for bug 17072 -->
//
// <-- GitLab URL -->
// https://gitlab.com/scilab/scilab/-/issues/17072
//
// <-- Short Description -->
// HTTP functions does not manage cookies

mode = xmlGetValues("//web/body/cookies", "mode");
// private mode
xmlSetValues("//web/body/cookies", ["mode"; "2"]);
try
    http_get("https://3ds.com", follow=%t);
catch
    // reset mode
    xmlSetValues("//web/body/cookies", ["mode"; mode]);
    error(lasterror());
end

// reset mode
xmlSetValues("//web/body/cookies", ["mode"; mode]);
// get lines where there is the string "3ds.com"
found = strstr(mgetl(TMPDIR+"/cookies.txt"), "3ds.com");
assert_checkfalse(isempty(found(found <> "")));