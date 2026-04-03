// =============================================================================
// Scilab ( http://www.scilab.org/ ) - This file is part of Scilab
// Copyright (C) 2024 - Dassault Systèmes S.E. - Adeline CARNIS
//
//  This file is distributed under the same license as the Scilab package.
// =============================================================================

// <-- CLI SHELL MODE -->
// <-- NO CHECK REF -->

// unit tests for dbscan function
// =============================================================================

rand("seed", 0)
x = [1 1.2 0.8 3.7 3.9 3.6 10; 1.1 0.8 1 4 3.9 4.1 10]';
l = dbscan(x, 0.5, 2);

assert_checkequal(l, [1 1 1 2 2 2 -1]');

n = 10;
X1 = [rand(n, 2)+1; rand(n, 2) + 5];
[l, core_idx] = dbscan(X1, 0.3, 4);

assert_checkequal(size(l), [20 1]);
assert_checkequal(unique(l), [-1;1;2]);
assert_checkequal(core_idx, [6 11 13 14 15 18 19 20]);

X = [0 0; 0 1; 1 0; 5 5; 5 6; 6 5; 9 9; 9 10; 10 9];
labels = dbscan(X, 1, 2);

assert_checkequal(size(labels), [9 1]);
assert_checkequal(labels, [1 1 1 2 2 2 3 3 3]');

// checkerror
msg = msprintf(_("%s: Wrong number of input arguments: %d to %d expected.\n"), "dbscan", 1, 3);
assert_checkerror("dbscan()", msg);

msg = msprintf(_("%s: Wrong type for input argument #%d: Must be in ""%s"".\n"), "dbscan", 1, "double");
assert_checkerror("dbscan(""1"")", msg);

msg = msprintf(_("%s: Wrong type for input argument #%d: Must be in ""%s"".\n"), "dbscan", 2, "double");
assert_checkerror("dbscan(1, ""2"")", msg);

msg = msprintf(_("%s: Wrong value for input argument #%d: Positive numbers expected.\n"), "dbscan", 2);
assert_checkerror("dbscan(x, -1)", msg);

msg = msprintf(_("%s: Wrong type for input argument #%d: Must be in ""%s"".\n"), "dbscan", 3, "double");
assert_checkerror("dbscan(x, 1, ""1"")", msg);

msg = msprintf(_("%s: Wrong value for input argument #%d: Integer numbers expected.\n"), "dbscan", 3);
assert_checkerror("dbscan(x, 1, 0.5)", msg);
