use std::collections::HashMap;
use std::sync::Arc;

use deltalake::arrow::array::Array as ArrowArray;
use deltalake::arrow::array::{
    BinaryArray as ArrowBinaryArray, BooleanArray as ArrowBooleanArray, BooleanBufferBuilder,
    Float64Array as ArrowFloat64Array, Int64Array as ArrowInt64Array,
    LargeBinaryArray as ArrowLargeBinaryArray, LargeListArray as ArrowLargeListArray,
    ListArray as ArrowListArray, StringArray as ArrowStringArray, StructArray as ArrowStructArray,
    TimestampMicrosecondArray as ArrowTimestampArray,
};
use deltalake::arrow::buffer::{NullBuffer, OffsetBuffer, ScalarBuffer};
use deltalake::arrow::datatypes::{
    DataType as ArrowDataType, Field as ArrowField, Fields as ArrowFields, Schema as ArrowSchema,
    TimeUnit as ArrowTimeUnit,
};
use ndarray::ArrayD;

use super::{LakeWriterSettings, MaintenanceMode};
use crate::connectors::data_format::{
    NDARRAY_ELEMENTS_FIELD_NAME, NDARRAY_SHAPE_FIELD_NAME, NDARRAY_SINGLE_ELEMENT_FIELD_NAME,
};
use crate::connectors::data_lake::LakeBatchWriter;
use crate::connectors::WriteError;
use crate::engine::time::DateTime as EngineDateTime;
use crate::engine::value::Handle;
use crate::engine::{Type, Value};
use crate::python_api::ValueField;

pub fn array_for_type(
    type_: &ArrowDataType,
    values: &[Value],
) -> Result<Arc<dyn ArrowArray>, WriteError> {
    match type_ {
        ArrowDataType::Boolean => {
            let v = array_of_simple_type::<bool>(values, |v| match v {
                Value::Bool(b) => Ok(*b),
                _ => Err(WriteError::TypeMismatchWithSchema(v.clone(), type_.clone())),
            })?;
            Ok(Arc::new(ArrowBooleanArray::from(v)))
        }
        ArrowDataType::Int64 => {
            let v = array_of_simple_type::<i64>(values, |v| match v {
                Value::Int(i) => Ok(*i),
                Value::Duration(d) => Ok(d.microseconds()),
                _ => Err(WriteError::TypeMismatchWithSchema(v.clone(), type_.clone())),
            })?;
            Ok(Arc::new(ArrowInt64Array::from(v)))
        }
        ArrowDataType::Float64 => {
            let v = array_of_simple_type::<f64>(values, |v| match v {
                Value::Float(f) => Ok((*f).into()),
                _ => Err(WriteError::TypeMismatchWithSchema(v.clone(), type_.clone())),
            })?;
            Ok(Arc::new(ArrowFloat64Array::from(v)))
        }
        ArrowDataType::Utf8 => {
            let v = array_of_simple_type::<String>(values, |v| match v {
                Value::String(s) => Ok(s.to_string()),
                Value::Pointer(p) => Ok(p.to_string()),
                Value::Json(j) => Ok(j.to_string()),
                _ => Err(WriteError::TypeMismatchWithSchema(v.clone(), type_.clone())),
            })?;
            Ok(Arc::new(ArrowStringArray::from(v)))
        }
        ArrowDataType::Binary | ArrowDataType::LargeBinary => {
            let mut vec_owned = array_of_simple_type::<Vec<u8>>(values, |v| match v {
                Value::Bytes(b) => Ok(b.to_vec()),
                Value::PyObjectWrapper(_) | Value::Pointer(_) => {
                    Ok(bincode::serialize(v).map_err(|e| *e)?)
                }
                _ => Err(WriteError::TypeMismatchWithSchema(v.clone(), type_.clone())),
            })?;
            let mut vec_refs = Vec::new();
            for item in &mut vec_owned {
                vec_refs.push(item.as_mut().map(|v| v.as_slice()));
            }
            if *type_ == ArrowDataType::Binary {
                Ok(Arc::new(ArrowBinaryArray::from(vec_refs)))
            } else {
                Ok(Arc::new(ArrowLargeBinaryArray::from(vec_refs)))
            }
        }
        ArrowDataType::Timestamp(ArrowTimeUnit::Microsecond, None) => {
            let v = array_of_simple_type::<i64>(values, |v| match v {
                #[allow(clippy::cast_possible_truncation)]
                Value::DateTimeNaive(dt) => Ok(dt.timestamp_microseconds()),
                _ => Err(WriteError::TypeMismatchWithSchema(v.clone(), type_.clone())),
            })?;
            Ok(Arc::new(ArrowTimestampArray::from(v)))
        }
        ArrowDataType::Timestamp(ArrowTimeUnit::Microsecond, Some(tz)) => {
            let v = array_of_simple_type::<i64>(values, |v| match v {
                #[allow(clippy::cast_possible_truncation)]
                Value::DateTimeUtc(dt) => Ok(dt.timestamp_microseconds()),
                _ => Err(WriteError::TypeMismatchWithSchema(v.clone(), type_.clone())),
            })?;
            Ok(Arc::new(ArrowTimestampArray::from(v).with_timezone(&**tz)))
        }
        ArrowDataType::List(nested_type) => array_of_lists(values, nested_type, false),
        ArrowDataType::LargeList(nested_type) => array_of_lists(values, nested_type, true),
        ArrowDataType::Struct(nested_struct) => array_of_structs(values, nested_struct.as_ref()),
        _ => panic!("provided type {type_} is unknown to the engine"),
    }
}

fn array_of_simple_type<ElementType>(
    values: &[Value],
    mut to_simple_type: impl FnMut(&Value) -> Result<ElementType, WriteError>,
) -> Result<Vec<Option<ElementType>>, WriteError> {
    let mut values_vec: Vec<Option<ElementType>> = Vec::new();
    for value in values {
        if matches!(value, Value::None) {
            values_vec.push(None);
            continue;
        }
        values_vec.push(Some(to_simple_type(value)?));
    }
    Ok(values_vec)
}

fn array_of_structs(
    values: &[Value],
    nested_types: &[Arc<ArrowField>],
) -> Result<Arc<dyn ArrowArray>, WriteError> {
    // Step 1. Decompose struct into separate columns
    let mut struct_columns: Vec<Vec<Value>> = vec![Vec::new(); nested_types.len()];
    let mut defined_fields_map = BooleanBufferBuilder::new(values.len());
    defined_fields_map.resize(values.len());
    for (index, value) in values.iter().enumerate() {
        defined_fields_map.set_bit(index, value != &Value::None);
        match value {
            Value::None => {
                for item in &mut struct_columns {
                    item.push(Value::None);
                }
            }
            Value::IntArray(a) => {
                struct_columns[0].push(convert_shape_to_pathway_tuple(a.shape()));
                struct_columns[1].push(convert_contents_to_pathway_tuple(a));
            }
            Value::FloatArray(a) => {
                struct_columns[0].push(convert_shape_to_pathway_tuple(a.shape()));
                struct_columns[1].push(convert_contents_to_pathway_tuple(a));
            }
            Value::Tuple(tuple_elements) => {
                for (index, field) in tuple_elements.iter().enumerate() {
                    struct_columns[index].push(field.clone());
                }
            }
            _ => panic!("Pathway type {value} is not serializable as an arrow tuple"),
        }
    }

    // Step 2. Create Arrow arrays for the separate columns
    let mut arrow_arrays = Vec::new();
    for (struct_column, arrow_field) in struct_columns.iter().zip(nested_types) {
        let arrow_array = array_for_type(arrow_field.data_type(), struct_column)?;
        arrow_arrays.push(arrow_array);
    }

    // Step 3. Create a struct array
    let struct_array: Arc<dyn ArrowArray> = Arc::new(ArrowStructArray::new(
        nested_types.into(),
        arrow_arrays,
        Some(NullBuffer::new(defined_fields_map.finish())),
    ));
    Ok(struct_array)
}

fn convert_shape_to_pathway_tuple(shape: &[usize]) -> Value {
    let tuple_contents: Vec<_> = shape
        .iter()
        .map(|v| Value::Int((*v).try_into().unwrap()))
        .collect();
    Value::Tuple(tuple_contents.into())
}

fn convert_contents_to_pathway_tuple<T: Into<Value> + Clone>(contents: &Handle<ArrayD<T>>) -> Value
where
    Value: std::convert::From<T>,
{
    let tuple_contents: Vec<_> = contents.iter().map(|v| Value::from((*v).clone())).collect();
    Value::Tuple(tuple_contents.into())
}

fn array_of_lists(
    values: &[Value],
    nested_type: &Arc<ArrowField>,
    use_64bit_size_type: bool,
) -> Result<Arc<dyn ArrowArray>, WriteError> {
    let mut flat_values = Vec::new();
    let mut offsets = Vec::new();

    let mut defined_fields_map = BooleanBufferBuilder::new(values.len());
    defined_fields_map.resize(values.len());
    for (index, value) in values.iter().enumerate() {
        offsets.push(flat_values.len());
        let Value::Tuple(list) = value else {
            defined_fields_map.set_bit(index, false);
            continue;
        };
        defined_fields_map.set_bit(index, true);
        for nested_value in list.as_ref() {
            flat_values.push(nested_value.clone());
        }
    }
    offsets.push(flat_values.len());

    let flat_values = array_for_type(nested_type.data_type(), &flat_values)?;
    let null_buffer = Some(NullBuffer::new(defined_fields_map.finish()));

    let list_array: Arc<dyn ArrowArray> = if use_64bit_size_type {
        let offsets: Vec<i64> = offsets.into_iter().map(|v| v.try_into().unwrap()).collect();
        let scalar_buffer = ScalarBuffer::from(offsets);
        let offset_buffer = OffsetBuffer::new(scalar_buffer);
        Arc::new(ArrowLargeListArray::new(
            nested_type.clone(),
            offset_buffer,
            flat_values,
            null_buffer,
        ))
    } else {
        let offsets: Vec<i32> = offsets.into_iter().map(|v| v.try_into().unwrap()).collect();
        let scalar_buffer = ScalarBuffer::from(offsets);
        let offset_buffer = OffsetBuffer::new(scalar_buffer);
        Arc::new(ArrowListArray::new(
            nested_type.clone(),
            offset_buffer,
            flat_values,
            null_buffer,
        ))
    };

    Ok(list_array)
}

fn arrow_data_type(
    type_: &Type,
    settings: &LakeWriterSettings,
) -> Result<ArrowDataType, WriteError> {
    Ok(match type_ {
        Type::Bool => ArrowDataType::Boolean,
        Type::Int | Type::Duration => ArrowDataType::Int64,
        Type::Float => ArrowDataType::Float64,
        Type::String | Type::Json | Type::Pointer => ArrowDataType::Utf8,
        Type::Bytes | Type::PyObjectWrapper => {
            if settings.use_64bit_size_type {
                ArrowDataType::LargeBinary
            } else {
                ArrowDataType::Binary
            }
        }
        // DeltaLake timestamps are stored in microseconds:
        // https://docs.rs/deltalake/latest/deltalake/kernel/enum.PrimitiveType.html#variant.Timestamp
        Type::DateTimeNaive => ArrowDataType::Timestamp(ArrowTimeUnit::Microsecond, None),
        Type::DateTimeUtc => ArrowDataType::Timestamp(
            ArrowTimeUnit::Microsecond,
            Some(settings.utc_timezone_name.clone().into()),
        ),
        Type::Optional(wrapped) => return arrow_data_type(wrapped, settings),
        Type::List(wrapped_type) => {
            let wrapped_type_is_optional = wrapped_type.is_optional();
            let wrapped_arrow_type = arrow_data_type(wrapped_type, settings)?;
            let list_field = ArrowField::new(
                NDARRAY_SINGLE_ELEMENT_FIELD_NAME,
                wrapped_arrow_type,
                wrapped_type_is_optional,
            );
            ArrowDataType::List(list_field.into())
        }
        Type::Array(_, wrapped_type) => {
            let wrapped_type = wrapped_type.as_ref();
            let elements_arrow_type = match wrapped_type {
                Type::Int => ArrowDataType::Int64,
                Type::Float => ArrowDataType::Float64,
                _ => panic!("Type::Array can't contain elements of the type {wrapped_type:?}"),
            };
            let struct_fields_vector = vec![
                ArrowField::new(
                    NDARRAY_SHAPE_FIELD_NAME,
                    ArrowDataType::List(
                        ArrowField::new(
                            NDARRAY_SINGLE_ELEMENT_FIELD_NAME,
                            ArrowDataType::Int64,
                            true,
                        )
                        .into(),
                    ),
                    false,
                ),
                ArrowField::new(
                    NDARRAY_ELEMENTS_FIELD_NAME,
                    ArrowDataType::List(
                        ArrowField::new(
                            NDARRAY_SINGLE_ELEMENT_FIELD_NAME,
                            elements_arrow_type,
                            true,
                        )
                        .into(),
                    ),
                    false,
                ),
            ];
            let struct_fields = ArrowFields::from(struct_fields_vector);
            ArrowDataType::Struct(struct_fields)
        }
        Type::Tuple(wrapped_types) => {
            let mut struct_fields = Vec::new();
            for (index, wrapped_type) in wrapped_types.iter().enumerate() {
                let nested_arrow_type = arrow_data_type(wrapped_type, settings)?;
                let nested_type_is_optional = wrapped_type.is_optional();
                struct_fields.push(ArrowField::new(
                    format!("[{index}]"),
                    nested_arrow_type,
                    nested_type_is_optional,
                ));
            }
            let struct_descriptor = ArrowFields::from(struct_fields);
            ArrowDataType::Struct(struct_descriptor)
        }
        Type::Any | Type::Future(_) => return Err(WriteError::UnsupportedType(type_.clone())),
    })
}

pub fn construct_schema(
    value_fields: &[ValueField],
    writer: &dyn LakeBatchWriter,
    mode: MaintenanceMode,
) -> Result<ArrowSchema, WriteError> {
    let settings = writer.settings();
    let metadata_per_column = writer.metadata_per_column();
    let mut schema_fields: Vec<ArrowField> = Vec::new();
    for field in value_fields {
        let metadata = metadata_per_column
            .get(&field.name)
            .unwrap_or(&HashMap::new())
            .clone();
        schema_fields.push(
            ArrowField::new(
                field.name.clone(),
                arrow_data_type(&field.type_, &settings)?,
                field.type_.can_be_none(),
            )
            .with_metadata(metadata),
        );
    }
    for (field, type_) in mode.additional_output_fields() {
        let metadata = metadata_per_column
            .get(field)
            .unwrap_or(&HashMap::new())
            .clone();
        schema_fields.push(
            ArrowField::new(field, arrow_data_type(&type_, &settings)?, false)
                .with_metadata(metadata),
        );
    }
    Ok(ArrowSchema::new(schema_fields))
}
