(* ::Package:: *)

(* ::Title:: *)
(*Sample Package for Testing*)

(* ::Section:: *)
(*Setup*)

(* ::Input:: *)
x = 5

(* ::Input:: *)
f[n_] := n^2 + x

(* ::Section:: *)
(*Computation*)

(* ::Input:: *)
Table[f[i], {i, 1, 10}]

(* ::Text:: *)
(*This computes f for the first 10 integers.*)

(* ::Input:: *)
Total[Table[f[i], {i, 1, 10}]]
